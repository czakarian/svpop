"""
Code for merging variant sets.
"""

import collections
import intervaltree
import multiprocessing
import numpy as np
import pandas as pd
import re
import sys
import traceback

import svpoplib
import kanapy

ALIGN_PARAM_FIELD_LIST = [
    ('SCORE-PROP', 0.8, float),   # Minimum proportion of the maximum possible score (all bases aligned)
    ('MATCH', 2.0, float),        # Match score
    ('MISMATCH', -1.0, float),    # Mismatch score
    ('GAP-OPEN', -5.0, float),    # Gap open score
    ('GAP-EXTEND', -0.5, float),  # Gap extend score
    ('MAP-LIMIT', 20000, int),    # Fall back to Jaccard index after this size.
    ('JACCARD-KMER', 9, int)      # K-mer size for Jaccard index (Using Jasmine default)
]

ALIGN_PARAM_KEY = {
    i: ALIGN_PARAM_FIELD_LIST[i][0] for i in range(len(ALIGN_PARAM_FIELD_LIST))
}

ALIGN_PARAM_DEFAULT = {
    ALIGN_PARAM_FIELD_LIST[i][0]: ALIGN_PARAM_FIELD_LIST[i][1] for i in range(len(ALIGN_PARAM_FIELD_LIST))
}

ALIGN_PARAM_TYPE = {
    ALIGN_PARAM_FIELD_LIST[i][0]: ALIGN_PARAM_FIELD_LIST[i][2] for i in range(len(ALIGN_PARAM_FIELD_LIST))
}


def get_merge_def(def_name, config, default_none=False):
    """
    Get a merge definition string from a configured alias, `def_name`. Returns
    `config['merge_def'][def_name]` if it exists and `config` is not `None`, and returns `None` otherwise.

    :param def_name: Definition name to search.
    :param config: Configuration dictionary (or `None`).
    :param default_none: Return `None` if there is no definition alias (default is to return `def_name`).

    :return: Configuration definition or `def_name` if not found.
    """

    if config is None or 'merge_def' not in config:
        if default_none:
            return None

        return def_name

    return config['merge_def'].get(def_name, None if default_none else def_name)


def merge_variants(bed_list, sample_names, strategy, fa_list=None, subset_chrom=None, threads=1):
    """
    Merge variants from multiple samples.

    :param bed_list: List of BED files to merge where each BED file is from one samples.
    :param sample_names: List of samples names. Each element matches an element at the same location in
        `bed_list`.
    :param strategy: Describes how to merge variants.
    :param fa_list: List of FASTA files (variant ID is FASTA record ID). Needed if sequences are used during merging.
    :param subset_chrom: Merge only records from this chromosome. If `None`, merge all records.
    :param threads: Number of threads to use for intersecting variants.

    :return: A Pandas dataframe of a BED file with the index set to the ID column.
    """

    strategy_tok = strategy.split(':', 1)

    if len(bed_list) != len(sample_names):
        raise RuntimeError('Sample name list length ({}) does not match the input file list length ({})'.format(
            len(sample_names), len(bed_list))
        )

    if len(bed_list) == 0:
        raise RuntimeError('Cannot merge 0 samples')

    if len(strategy_tok) == 1:
        strategy_tok.append(None)

    if strategy_tok[0] == 'nr':
        return merge_variants_nr(bed_list, sample_names, strategy_tok[1], fa_list=fa_list, subset_chrom=subset_chrom, threads=threads)

    else:
        raise RuntimeError('Unrecognized strategy: {}'.format(strategy))


def merge_variants_nr(bed_list, sample_names, merge_params, fa_list=None, subset_chrom=None, threads=1):
    """
    Merge all non-redundant variants from multiple samples.

    Recognized parameters:
    * ro: Match variants by reciprocal overlap.
    * szro: Match variants minimum reciprocal size overlap (max value for ro). This is like ro, but allows the variants
        to be offset. Size overlap is calculated only on variant sizes, and a maximum offset (offset parameter)\
        determines how far apart the variants may be. If szro is specified and ro is not, then ro is implied with the
        same overlap threshold and tried before size/offset overlap.
    * offset: Maximum offset (minimum of start position difference or end position difference)
    * ref, alt, and refalt: If specified, the REF and/or ALT columns must match. This is an additional restriction on
        other merging parameters (is not a merging strategy in itself).

    Merging each BED occurs in these stages:
        1) ID. Same ID is always matched first (no parameters needed to specify). IDs in SV-Pop are assumed to be
            a descriptor of the variant including chrom, position, size. If IDs match, REF and ALT are assumed to also
            match.
        2) RO: If ro and/or szro is set, then a strict reciprocal-overlap is run. The minimum overlap is ro if it was
            defined and szro if ro was not defined.
        3) Size-overlap + offset: Try intersecting by reciprocal-size-overlap with a maximum offset.

    :param bed_list: List of BED files to merge where each BED file is from one samples.
    :param sample_names: List of samples names. Each element matches an element at the same location in
        `bed_list`.
    :param merge_params: Overlap percentage or None to merge by the default (50% overlap).
    :param fa_list: List of FASTA files matching `bed_list` and `sample_names`. FASTA files contain sequences for
        variants where each FASTA record ID is the variant ID and the sequence is the variant sequence.
    :param subset_chrom: Merge only records from this chromosome. If `None`, merge all records.
    :param threads: Number of threads to use for intersecting variants.

    :return: A Pandas dataframe of a BED file with the index set to the ID column.
    """

    merge_strategy = 'nr'

    # Check BED
    if len(bed_list) == 0:
        raise ValueError('BED file list is empty')

    # Check sample names
    if sample_names is None or len(sample_names) == 0:
        raise RuntimeError('Sample names is missing or empty')

    sample_names = [val.strip() for val in sample_names]

    if not all(bool(val) for val in sample_names):
        raise RuntimeError('Found empty sample names')

    if any([re.match('.*\\s.*', val) is not None for val in sample_names]):
        raise RuntimeError('Error: Sample names contain whitespace')

    n_samples = len(sample_names)

    if len(set(sample_names)) != n_samples:
        raise RuntimeError('Sample names may not be duplicated')

    # Parse parameters
    param_set = get_param_set(merge_params, merge_strategy)

    # Check fa_list if sequences are required
    seq_in_col = False

    if param_set.read_seq:
        if fa_list is None:
            seq_in_col = True
            fa_list = [None] * n_samples

        elif len(fa_list) != n_samples:
            n_fa = len(fa_list)

            raise RuntimeError(f'Non-redundant merge requires variant sequences, but fa_list is not the same length as the number of samples: expected {n_samples}, fa_list length {n_fa}')

    else:
        fa_list = [None] * n_samples

    # Set required columns for variant DataFrames
    col_list = ['#CHROM', 'POS', 'END', 'ID', 'SVTYPE', 'SVLEN']

    if param_set.match_ref:
        col_list += ['REF']

    if param_set.match_alt:
        col_list += ['ALT']

    if param_set.read_seq:
        col_list += ['SEQ']


    # Note:
    # The first time a variant is found, it is added to the merge set (df), and it is supported by itself. The overlap
    # score is defined as -1. As variants are added that intersect with a variant already in the merge set, SAMPLE and
    # ID are copied from that variant, the supporting variant is noted in SUPPORT_ID and SUPPORT_SAMPLE, and the overlap
    # score is between 0 and 1 (depending on how well they overlap).

    # Initialize a table of variants with the first sample. All variants from this sample are in the merged set.
    sample_name = sample_names[0]

    print('Merging: {}'.format(sample_name))

    df = read_variant_table(bed_list[0], sample_name, subset_chrom, fa_list[0], col_list)

    if seq_in_col:
        if 'SEQ' not in df.columns:
            raise RuntimeError(f'Merge requires variant sequences, but none were provided through FASTA files or as SEQ columns: {bed_list[0]}')

    # Add tracking columns
    df['SAMPLE'] = sample_name

    df['SUPPORT_ID'] = df['ID']
    df['SUPPORT_SAMPLE'] = sample_name

    df['SUPPORT_OFFSET'] = -1
    df['SUPPORT_RO'] = -1
    df['SUPPORT_SZRO'] = -1
    df['SUPPORT_OFFSZ'] = -1
    df['SUPPORT_MATCH'] = -1

    # Setup a dictionary to translate support sample table columns to the merged table columns
    support_col_rename = {
        'OFFSET': 'SUPPORT_OFFSET',
        'RO': 'SUPPORT_RO',
        'SZRO': 'SUPPORT_SZRO',
        'OFFSZ': 'SUPPORT_OFFSZ',
        'MATCH': 'SUPPORT_MATCH'
    }

    # Add each variant to the table
    base_support = list()  # Table of supporting variants if they are not part of subsequent rounds of intersection (defined by "expand").

    for index in range(1, len(bed_list)):

        ## Read ##

        # Read next variant table
        sample_name = sample_names[index]

        print('Merging: {}'.format(sample_name))

        df_next = read_variant_table(bed_list[index], sample_name, subset_chrom, fa_list[index], col_list)

        if seq_in_col:
            if 'SEQ' not in df_next.columns:
                raise RuntimeError(f'Merge requires variant sequences, but none were provided through FASTA files or as SEQ columns: {bed_list[index]}')

        ## Build intersect support table ##
        support_table_list = list()

        # Create copies of df and df_next that can be subset (variants removed by each phase of intersection)
        df_sub = df.copy()
        df_next_sub = df_next.copy()

        # INTERSECT: Exact match
        support_table_list.append(
            get_support_table_exact(
                df_sub, df_next_sub,
                param_set.match_seq,
                param_set.match_ref, param_set.match_alt
            )
        )

        id_set = set(support_table_list[-1]['ID'])
        id_next_set = set(support_table_list[-1]['TARGET_ID'])

        df_sub = df_sub.loc[df_sub['ID'].apply(lambda val: val not in id_set)]
        df_next_sub = df_next_sub.loc[df_next_sub['ID'].apply(lambda val: val not in id_next_set)]

        # INTERSECT: RO (ro or szro defined)
        if param_set.ro_min is not None and df_sub.shape[0] > 0 and df_next_sub.shape[0] > 0:

            df_support_ro = get_support_table(
                df_sub, df_next_sub,
                threads,
                None,
                param_set.ro_min, param_set.match_ref, param_set.match_alt,
                param_set.aligner, param_set.align_match_prop
            )

            id_set = set(df_support_ro['ID'])
            id_next_set = set(df_support_ro['TARGET_ID'])

            df_sub = df_sub.loc[df_sub['ID'].apply(lambda val: val not in id_set)]
            df_next_sub = df_next_sub.loc[df_next_sub['ID'].apply(lambda val: val not in id_next_set)]

            support_table_list.append(df_support_ro)

            del(df_support_ro)

        # INTERSECT: SZRO + OFFSET
        if param_set.szro_min is not None and df_sub.shape[0] > 0 and df_next_sub.shape[0] > 0:

            df_support_szro = get_support_table(
                df_sub, df_next_sub,
                threads,
                param_set.offset_max,
                param_set.ro_min, param_set.match_ref, param_set.match_alt,
                param_set.aligner, param_set.align_match_prop
            )

            df_support_szro = df_support_szro.loc[(df_support_szro['OFFSET'] <= param_set.offset_max) & (df_support_szro['SZRO'] >= param_set.szro_min)]

            id_set = set(df_support_szro['ID'])
            id_next_set = set(df_support_szro['TARGET_ID'])

            df_sub = df_sub.loc[df_sub['ID'].apply(lambda val: val not in id_set)]
            df_next_sub = df_next_sub.loc[df_next_sub['ID'].apply(lambda val: val not in id_next_set)]

            support_table_list.append(df_support_szro)

            del(df_support_szro)

        # Concat df_support
        df_support = pd.concat(support_table_list)

        del(df_sub)
        del(df_next_sub)

        # Annotate support
        if df_support.shape[0] > 0:

            # Set Index
            df_support.set_index('TARGET_ID', inplace=True, drop=False)
            df_support.index.name = 'INDEX'

            # Add sample name
            df_support['SAMPLE'] = sample_name
            df_support['SUPPORT_SAMPLE'] = list(df.loc[df_support['ID'], 'SAMPLE'])

            # Fix IDs. ID = new ID. SUPPORT_ID = ID in sample it supports (merged callset ID)
            df_support['SUPPORT_ID'] = list(df.loc[df_support['ID'], 'SUPPORT_ID'])
            df_support['ID'] = df_support['TARGET_ID']

            # Rename columns (offset merger column names to support column names)
            df_support.columns = [support_col_rename.get(col, col) for col in df_support.columns]

            # Remove redundant support (if present).
            df_support = df_support.sort_values(
                ['SUPPORT_RO', 'SUPPORT_OFFSET', 'SUPPORT_SZRO', 'SUPPORT_MATCH'],
                ascending=[False, True, False, False]
            ).drop_duplicates('ID', keep='first')

            # Arrange columns
            df_support['#CHROM'] = df_next['#CHROM']
            df_support['POS'] = df_next['POS']
            df_support['END'] = df_next['END']
            df_support['SVTYPE'] = df_next['SVTYPE']
            df_support['SVLEN'] = df_next['SVLEN']

            if 'REF' in df_next.columns:
                df_support['REF'] = df_next['REF']

            if 'ALT' in df_next.columns:
                df_support['ALT'] = df_next['ALT']

            df_support = df_support.loc[:, [col for col in df.columns if col != 'SEQ']]

            # Append to existing supporting variants (expand) or save to a list of support (no expand)
            if param_set.expand_base:
                if 'SEQ' in df.columns:
                    df_support_seq = df_support.copy()
                    df_support_seq['SEQ'] = df_next['SEQ']

                    df = pd.concat([df, df_support_seq], axis=0)

                else:
                    df = pd.concat([df, df_support], axis=0)
            else:
                base_support.append(df_support)

        # Read new variants from this sample (variants that do not support an existing call)
        support_id_set = set(df_support['ID'])

        df_new = df_next.loc[df_next['ID'].apply(lambda val: val not in support_id_set)].copy()

        if df_new.shape[0] > 0:
            df_new['SUPPORT_ID'] = df_new['ID']
            df_new['SAMPLE'] = sample_name
            df_new['SUPPORT_SAMPLE'] = sample_name
            df_new['SUPPORT_OFFSET'] = -1
            df_new['SUPPORT_RO'] = -1
            df_new['SUPPORT_SZRO'] = -1
            df_new['SUPPORT_OFFSZ'] = -1
            df_new['SUPPORT_MATCH'] = -1

            # De-duplicate IDs
            df_new['SUPPORT_ID'] = svpoplib.variant.version_id(df_new['SUPPORT_ID'], set(df['SUPPORT_ID']))
            df_new.set_index('SUPPORT_ID', inplace=True, drop=False)

            # Append new variants
            df = pd.concat([df, df_new.loc[:, df.columns]], axis=0)

        # Sort
        df.sort_values(['#CHROM', 'POS', 'SVLEN', 'ID'], inplace=True)

    # Merge support variants into df (where support tables are kept if df is not expanded)
    if 'SEQ' in df.columns:
        del(df['SEQ'])

    if len(base_support) > 0:

        if len(base_support) > 1:
            df_base_support = pd.concat(base_support, axis=0)
        else:
            df_base_support = base_support[0]

        df = pd.concat([df, df_base_support], axis=0)

    # Finalize merged variant set
    if df.shape[0] > 0:

        # Make SAMPLE and SUPPORT_SAMPLE categorical (sort in the same order as they were merged)
        df['SAMPLE'] = pd.Categorical(df['SAMPLE'], sample_names)
        df['SUPPORT_SAMPLE'] = pd.Categorical(df['SUPPORT_SAMPLE'], sample_names)

        # Sort by support (best support first)
        df['SUPPORT_OFFSET'] = df['SUPPORT_OFFSET'].apply(lambda val: np.max([0, val]))
        df['SUPPORT_RO'] = np.abs(df['SUPPORT_RO'])
        df['SUPPORT_SZRO'] = np.abs(df['SUPPORT_SZRO'])
        df['SUPPORT_OFFSZ'] = np.abs(df['SUPPORT_OFFSZ'])
        df['SUPPORT_MATCH'] = np.abs(df['SUPPORT_MATCH'])

        df['IS_PRIMARY'] = df['SAMPLE'] == df['SUPPORT_SAMPLE']

        df.sort_values(
            ['SAMPLE', 'SUPPORT_RO', 'SUPPORT_OFFSET', 'SUPPORT_SZRO', 'SUPPORT_OFFSZ', 'SUPPORT_MATCH'],
            ascending=[True, False, True, False, False, False],
            inplace=True
        )

        # Find best support variant for each mergeset variant (per sample)
        df.drop_duplicates(['SUPPORT_ID', 'SAMPLE', 'SUPPORT_SAMPLE'], keep='first', inplace=True)

        # Re-sort by ID then SAMPLE (for concatenating stats in order)
        df.sort_values(['SUPPORT_ID', 'SAMPLE', 'SUPPORT_SAMPLE'], inplace=True)

        df_support = df.groupby(
            'SUPPORT_ID'
        ).apply(lambda subdf: pd.Series(
            [
                subdf.iloc[0]['SUPPORT_SAMPLE'],
                #subdf.loc[subdf['SUPPORT_ID']].iloc[0].squeeze()['ORG_ID'],
                subdf.iloc[0].squeeze()['ID'],

                subdf.shape[0],
                '{:.4f}'.format(subdf.shape[0] / n_samples),

                ','.join(subdf['SAMPLE']),
                ','.join(subdf['ID']),

                ','.join(['{:.2f}'.format(val) for val in subdf['SUPPORT_RO']]),
                ','.join(['{:.0f}'.format(val) for val in subdf['SUPPORT_OFFSET']]),
                ','.join(['{:.2f}'.format(val) for val in subdf['SUPPORT_SZRO']]),
                ','.join(['{:.2f}'.format(val) for val in subdf['SUPPORT_OFFSZ']]),
                ','.join(['{:.2f}'.format(val) for val in subdf['SUPPORT_MATCH']])
            ],
            index=[
                'MERGE_SRC', 'MERGE_SRC_ID',
                'MERGE_AC', 'MERGE_AF',
                'MERGE_SAMPLES', 'MERGE_VARIANTS',
                'MERGE_RO', 'MERGE_OFFSET', 'MERGE_SZRO', 'MERGE_OFFSZ', 'MERGE_MATCH'
            ]
        ))

        df_support.reset_index(inplace=True, drop=False)

    else:
        df_support = pd.DataFrame(
            [],
            columns=[
                'MERGE_SRC', 'MERGE_SRC_ID', 'MERGE_AC', 'MERGE_AF',
                'SUPPORT_ID', 'MERGE_SAMPLES', 'MERGE_VARIANTS',
                'MERGE_RO', 'MERGE_OFFSET', 'MERGE_SZRO', 'MERGE_OFFSZ', 'MERGE_MATCH'
            ]
        )

    # Merge original BED files by by df_support
    return merge_sample_by_support(df_support, bed_list, sample_names)


def merge_annotations(df_merge, anno_tab_list, sample_names, sort_columns=None):
    """
    Merge a table of annotations from several samples into one table.

    :param df_merge: Table of merged structural variants (BED file). Must contain columns 'ID',
        'MERGE_SRC', and 'MERGE_SRC_ID'.
    :param anno_tab_list: List of annotations tables to be merged. Must have an 'ID' column.
    :param sample_names: Names of the samples to be merged. Each element is the name for the data in
        `anno_tab_list` at the same index.
    :param sort_columns: List of column names to sort the merged annotations by or `None` to leave it unsorted.

    :return: Dataframe of merged annotations.
    """

    # Check arguments
    if len(anno_tab_list) != len(sample_names):
        raise RuntimeError('Sample name list length ({}) does not match the input file list length ({})'.format(
            len(sample_names), len(anno_tab_list))
        )

    if len(anno_tab_list) == 0:
        raise RuntimeError('Cannot merge 0 samples')

    df_list = list()

    # Subset from each samples
    for index in range(len(sample_names)):

        # Get samples name and input file name
        sample_name = sample_names[index]
        anno_tab = anno_tab_list[index]

        # Get table of annotations
        df_anno = pd.read_csv(anno_tab, sep='\t', header=0)
        df_anno.index = df_anno['ID']

        # Subset
        df_merge_subset = df_merge.loc[df_merge['MERGE_SRC'] == sample_name]
        id_dict = {row[1]['MERGE_SRC_ID']: row[1]['ID'] for row in df_merge_subset.iterrows()}

        id_subset = set(df_merge_subset['MERGE_SRC_ID'])

        df_anno = df_anno.loc[df_anno['ID'].apply(lambda svid: svid in id_subset)]

        df_anno['ID'] = df_anno['ID'].apply(lambda svid: id_dict[svid])
        df_anno.index = df_anno['ID']

        df_list.append(df_anno)

    # Merge subsets
    df_anno = pd.concat(df_list, axis=0)

    # Resort
    if sort_columns is not None:
        df_anno.sort_values(list(sort_columns), inplace=True)

    # Return
    return df_anno


def get_samples_for_mergeset(mergeset, config):
    """
    Get a list of samples with source variants for a set of merged samples.

    :param mergeset: Merged variant set name.
    :param config: Config (loaded by Snakemake).

    :return: List of samples that must be loaded for `mergeset`.
    """

    # Get list of samples
    sample_list = config['svmerge'][mergeset].get('svsource', None)

    if sample_list is None:
        sample_set_name = config['svmerge'][mergeset]['sampleset']
        sample_list = config['sampleset'][sample_set_name]

        if len(sample_list) == 0:
            raise RuntimeError('Empty input for merge set {} after matching full samples set'.format(mergeset))

    else:
        if len(sample_list) == 0:
            raise RuntimeError('Empty input for merge set {}'.format(mergeset))

    # Return list
    return sample_list


def get_disc_class_by_row(row):
    """
    Get discovery class.

    :param row: A series with fields "MERGE_AF" and "MERGE_AC".

    :return: A class (as string).
    """

    if row['MERGE_AF'] == 1:
        return 'SHARED'

    if row['MERGE_AF'] >= 0.5:
        return 'MAJOR'

    if row['MERGE_AC'] > 1:
        return 'POLY'

    return 'SINGLE'


def get_disc_class(df):
    """
    Get discovery class.

    :param df: A dataframe with columns "MERGE_AF" and "MERGE_AC" or a series with those fields.

    :return: A class (as string) for each row (if dataframe) or a class (as string) if series.
    """

    # If df is series, get value for one row
    if df.__class__ is pd.core.series.Series:
        return get_disc_class_by_row(df)

    # Apply to all rows
    return df.apply(get_disc_class_by_row, axis=1)


def merge_sample_by_support(df_support, bed_list, sample_names):
    """
    Take a support table and generate the merged variant BED file.
    
    The support table must have at least columns:
    1) SUPPORT_ID: ID in the final merged callset. Might have been altered to avoid name clashes (e.g. "XXX.1").
    2) MERGE_SRC: Sample variant should be extracted from
    3) MERGE_SRC_ID: ID of of the variant to extract from the source. This becomes the record that represents all
        variants merged with it.
    
    The support table may also have:
    1) MERGE_AC: Number of samples variant was found in.
    2) MERGE_AF: MERGE_AC divided by the number of samples merged.
    3) MERGE_SAMPLES: A comma-separated list of samples. If present, number of samples must match MERGE_AC, and the
        first sample must be MERGE_SRC.
    4) MERGE_VARIANTS: A comma-separated list of variants from each sample. If present, number must match MERGE_AC and
        be in the same order as MERGE_SAMPLES.
    5) MERGE_RO: A comma-separated list of reciprocal-overlap values with the variant from each sample against the
        representitive variant. If present, number must match MERGE_AC and be in the same order as MERGE_SAMPLES.
    6) MERGE_OFFSET: A comma-separated list of offset distances with the variant from each sample against the
        representitive variant. If present, number must match MERGE_AC and be in the same order as MERGE_SAMPLES.
    7) MERGE_SZRO: A comma-separated list of size-reciprocal-overlap values with the variant from each sample against
        the representitive variant. If present, number must match MERGE_AC and be in the same order as MERGE_SAMPLES.
    8) MERGE_OFFSZ: A comma-separated list of offset/size values with the variant from each sample against the
        representitive variant. If present, number must match MERGE_AC and be in the same order as MERGE_SAMPLES.

    Other columns are silently ignored.
    
    :param df_support: Variant support table (see above for format). If `None`, treats this as as empty table and
        generates a merged dataframe with no variants.
    :param bed_list: List of input BED files.
    :param sample_names: List of sample names. Must be the same length as `bed_list` where values correspond (name at
        index X is the name of the sample in `bed_list` at index X).
    
    :return: A merged dataframe of variants.
    """

    REQUIRED_COLUMNS = ['SUPPORT_ID', 'MERGE_SRC', 'MERGE_SRC_ID']

    OPT_COL = ['MERGE_AC', 'MERGE_AF', 'MERGE_SAMPLES', 'MERGE_VARIANTS', 'MERGE_RO', 'MERGE_OFFSET', 'MERGE_SZRO', 'MERGE_OFFSZ', 'MERGE_MATCH']

    OPT_COL_DTYPE = {
        'MERGE_AC': np.int32,
        'MERGE_AF': np.float32
    }

    # Check values
    if df_support is None:
        df_support = pd.DataFrame([], columns=REQUIRED_COLUMNS)

    if len(bed_list) != len(sample_names):
        raise RuntimeError(
            'BED list and sample name list lengths differ ({} vs {})'.format(len(bed_list), len(sample_names))
        )

    if df_support.shape[0] > 0:
        missing_cols = [col for col in REQUIRED_COLUMNS if col not in df_support.columns]
    else:
        missing_cols = []

    if missing_cols:
        raise RuntimeError('Missing column(s) in df_support: ' + ', '.join(missing_cols))

    # Get optional columns that are present
    opt_columns = [opt_col for opt_col in OPT_COL if opt_col in df_support.columns]

    # Merged variant IDs should be unique
    #dup_id_list = [val for val, count in collections.Counter(df_support['MERGE_SRC_ID']).items() if count > 1]
    dup_id_list = [val for val, count in collections.Counter(df_support['SUPPORT_ID']).items() if count > 1]

    if len(dup_id_list) > 0:
        dup_id_str = ', '.join(dup_id_list[:3]) + (', ...' if len(dup_id_list) > 3 else '')
        raise RuntimeError('Duplicate IDs after merging ({}): {}'.format(len(dup_id_list), dup_id_str))

    # Merge original SVs
    col_list = list()  # List of columns in the final output
    merge_df_list = list()  # List of dataframes to be merged into final output

    for index in range(len(bed_list)):
        bed_file_name = bed_list[index]
        sample_name = sample_names[index]

        # Get merged variants for this sample
        df_support_sample = df_support.loc[df_support['MERGE_SRC'] == sample_name].copy()

        # Set index to original ID in this sample
        df_support_sample.set_index('MERGE_SRC_ID', inplace=True, drop=False)
        df_support_sample.index.name = 'INDEX'

        # Read variants from sample
        df_sample = pd.read_csv(bed_file_name, sep='\t', header=0)

        # Update columns
        col_list.extend([col for col in df_sample.columns if col not in col_list])

        # Get variant names
        if df_support_sample.shape[0] == 0:
            continue

        df_sample.set_index('ID', inplace=True, drop=True)
        df_sample.index.name = 'INDEX'

        df_sample = df_sample.loc[df_support_sample['MERGE_SRC_ID']]

        # Transfer required columns
        df_sample['ID'] = df_support_sample['SUPPORT_ID']  # ID in the final merged callset
        df_sample['MERGE_SRC'] = df_support_sample['MERGE_SRC']
        df_sample['MERGE_SRC_ID'] = df_support_sample['MERGE_SRC_ID']

        # Transfer optional columns annotations
        for col in opt_columns:
            df_sample[col] = df_support_sample[col]

        # Append to list
        merge_df_list.append(df_sample)

    # Append required and optional columns to the end
    col_list.extend([col for col in REQUIRED_COLUMNS if col not in col_list])
    col_list.extend([col for col in opt_columns if col not in col_list])

    # Create merged dataframe
    if merge_df_list:
        df_merge = pd.concat(merge_df_list, axis=0, sort=False)
    else:
        df_merge = pd.DataFrame([], columns=col_list)

    # Set column data types
    for col in opt_columns:
        if col in OPT_COL_DTYPE:
            df_merge[col] = df_merge[col].astype(OPT_COL_DTYPE[col])

    # Sort
    df_merge.sort_values(['#CHROM', 'POS'], inplace=True)

    # Get column order
    head_cols = ['#CHROM', 'POS', 'END', 'ID']

    if 'SVTYPE' in df_merge.columns:
        head_cols += ['SVTYPE']

    if 'SVLEN' in df_merge.columns:
        head_cols += ['SVLEN']

    tail_cols = [col for col in col_list if col not in head_cols]

    # Add missing columns (unique fields from a caller with no records, keeps it consistent with the whole callset)
    for col in col_list:
        if col not in df_merge.columns:
            if col in head_cols:
                raise RuntimeError(f'Missing head column while merging sample support (post-merge step to prepare final table): {col}')

            df_merge[col] = np.nan

    # Order columns
    df_merge = df_merge.loc[:, head_cols + tail_cols]

    df_merge.reset_index(drop=True, inplace=True)

    return df_merge


def get_param_set(merge_params, strategy):
    """
    Parse parameters and store as fields on an object. Throws exceptions if there are any problems with the parameters.

    :param merge_params: Parameter string.
    :param strategy: Merge strategy (for errors)

    :return: An object with fields set.
    """

    # Initialize parameters
    class param_set:
        pass

    # General parameters
    param_set.ro_min = None        # Reciprocal overlap threshold
    param_set.szro_min = None      # Size reciprocal-overlap threshold
    param_set.offset_max = None    # Max variant offset
    param_set.match_ref = False    # Match REF (exact match) if True
    param_set.match_alt = False    # Match ALT (exact match) if True
    param_set.expand_base = False  # If true, expand base variants with matches. The default is to use lead variants only (do not expand the base variants).
                                   # (e.g. Variants A, B, and C are in 3 different samples and merged in that order. A supports B, so B is added to the set of variants that can be matched.
                                   #  C can then support A by matching B even if C would not match A on its own).

    # Sequence match parameters
    param_set.match_seq = False        # If True, match sequences by a ligning and applying a match threshold
    param_set.aligner = None           # Configured alignger with match/mismatch/gap parameters set
    param_set.align_match_prop = None  # Match proprotion threshold (matching bases / sequence length)
    param_set.align_param = None       # Alignment parameters used to configure aligner

    # Read sequence
    param_set.read_seq = False  # If set, a parameter requires sequence-resolution as a SEQ column (flag to load SEQ from FASTA)

    # Check parameters
    if merge_params is None:
        raise RuntimeError(f'Cannot merge (strategy={strategy}) with parameters: None')

    # Split and parse
    for param_element in merge_params.split(':'):

        # Tokenize
        param_element = param_element.strip()

        if not param_element:
            continue

        param_tok = re.split('\s*=\s*', param_element, 1)

        key = param_tok[0].lower()

        if len(param_tok) > 1:
            val = param_tok[1]
        else:
            val = None

        # Process key
        if key == 'ro':

            if val is None:
                raise ValueError(f'Missing value for parameter "ro" (e.g. "ro=50"): {merge_params}')

            if val != 'any':
                param_set.ro_min = int(val.strip()) / 100

                if param_set.ro_min < 0 or param_set.ro_min > 1:
                    raise ValueError(
                        f'Overlap length (ro) must be between 0 and 100 (inclusive): {param_element}'
                    )

            else:
                raise RuntimeError('RO "any" is not yet implemented')

        elif key == 'szro':
            if val is None:
                raise ValueError(f'Missing value for parameter "szro" (e.g. "szro=50"): {merge_params}')

            if val != 'any':
                param_set.szro_min = int(val.strip()) / 100

                if param_set.szro_min < 0 or param_set.szro_min > 1:
                    raise ValueError(
                        f'Overlap length (szro) must be between 0 and 100 (inclusive): {param_element}'
                    )

            else:
                param_set.szro_min = None

        elif key == 'offset':
            if val is None:
                raise ValueError(f'Missing value for parameter "offset" (maxiumum offset, e.g. "offset=2000"): {merge_params}')

            if val != 'any':
                param_set.offset_max = int(val.strip())

                if param_set.offset_max < 0:
                    raise RuntimeError(f'Maximum offset (offset parameter) may not be negative: {merge_params}')

            else:
                param_set.offset_max = None

        elif key == 'refalt':
            if val is not None:
                raise RuntimeError(f'Match-REF/ALT (refalt) should not have an argument: {merge_params}')

            param_set.match_ref = True
            param_set.match_alt = True

        elif key == 'ref':
            if val is not None:
                raise RuntimeError(f'Match-REF (ref) should not have an argument: {merge_params}')

            param_set.match_ref = True

        elif key == 'alt':
            if val is not None:
                raise RuntimeError(f'Match-ALT (alt) should not have an argument: {merge_params}')

            param_set.match_alt = True

        elif key == 'expand':
            if val is not None:
                raise RuntimeError(f'Expand-base (expand) should not have an argument: {merge_params}')

            param_set.expand_base = True

        elif key == 'match':
            # Align arguments:
            # 0: Min score proportion
            # 1: Match
            # 2: Mismatch
            # 3: Gap open
            # 4: Gap extend
            # 5: Map limit ("NA" or "UNLIMITED" sets no limit). Fall back to Jaccard index after this limit.
            # 6: Jaccard k-mer size

            param_set.align_param = ALIGN_PARAM_DEFAULT.copy()

            if not val:
                val = ''

            val = val.strip()

            val_split = val.split(',')

            if len(val_split) > len(param_set.align_param):
                raise RuntimeError('Alignment parameter in "match" argument count {} exceeds max: {}'.format(len(val_split), len(param_set.align_param)))

            for i in range(len(val_split)):

                # Get token
                tok = val_split[i].strip()
                field_name = ALIGN_PARAM_KEY[i]
                param_type = ALIGN_PARAM_TYPE[field_name]

                if not tok:
                    continue  # Leave default unchanged

                # Get token value
                if field_name == 'MAP-LIMIT' and tok.lower in {'na', 'unlimited'}:
                    tok_val = None

                else:
                    try:
                        tok_val = param_type(tok)

                    except ValueError:
                        raise RuntimeError(f'Alignment parameter {i} in "match" type mismatch: Expected {param_type}: {tok}')

                    if field_name == 'SCORE-PROP':
                        if tok_val <= 0.0 or tok_val > 1:
                            raise RuntimeError(f'Alignment parameter {i} ({field_name}) in "match" must be between 0 (exclusive) and 1 (inclusive): {tok}')

                    elif field_name in {'MATCH', 'JACCARD-KMER'}:
                        if tok_val <= 0:
                            raise RuntimeError(f'Alignment parameter {i} ({field_name}) in "match" must be positive: {tok}')

                    elif field_name in {'MISMATCH', 'GAP-OPEN', 'GAP-EXTEND'}:
                        if tok_val > 0.0:
                            raise RuntimeError(f'Alignment parameter {i} ({field_name}) in "match" must not be positive: {tok}')

                    elif field_name == 'MAP-LIMIT':
                        if tok_val < 0:
                            raise RuntimeError(f'Alignment parameter {i} ({field_name}) in "match" must not be negative: {tok}')

                # Assign
                param_set.align_param[field_name] = tok_val

        else:
            raise ValueError(f'Unknown parameter token: {key}')

    # Check parameters
    if param_set.szro_min is not None and param_set.offset_max is None:
        raise RuntimeError('Parameters "szro" was specified without "offset"')

    # Get merge size threshold
    if param_set.ro_min is None and param_set.szro_min is not None:
        param_set.ro_min = param_set.szro_min

    # Set match_seq and aligner
    if param_set.align_param is not None:
        param_set.match_seq = True

        param_set.aligner = svpoplib.aligner.ScoreAligner(
            match=param_set.align_param['MATCH'],
            mismatch=param_set.align_param['MISMATCH'],
            gap_open=param_set.align_param['GAP-OPEN'],
            gap_extend=param_set.align_param['GAP-EXTEND']
        )

        param_set.align_match_prop = param_set.align_param['SCORE-PROP']

    else:
        param_set.match_seq = False
        param_set.aligner = None
        param_set.align_match_prop = None

    # Set read_seq
    param_set.read_seq = param_set.match_seq  # Future: read_seq may be set for other reasons, keep as a separate flag

    # Return parameters
    return param_set


def read_variant_table(
        bed_file_name,
        sample_name,
        subset_chrom=None,
        fa_file_name=None,
        col_list=('#CHROM', 'POS', 'END', 'ID', 'SVTYPE', 'SVLEN')
    ):
    """
    Read a DataFrame of variants and prepare for merging.

    :param bed_file_name: BED file name.
    :param sample_name: Sample name.
    :param subset_chrom: Subset to this chromosome (or None to read all).
    :param fa_file_name: FASTA file name to read variant sequences (into SEQ column), or `None` if variant sequences
        do not need to be read. FASTA record IDs must match the variant ID column.
    :param col_list: List of columns to be read. Used to order and filter columns.

    :return: Prepared variant DataFrame.
    """


    # Ensure column list contains required columns
    head_cols = ['#CHROM', 'POS', 'END', 'ID', 'SVTYPE', 'SVLEN']
    col_list = head_cols + [col for col in col_list if col not in head_cols]

    # Read variants
    col_set = set(col_list)

    df = svpoplib.pd.read_csv_chrom(
        bed_file_name, chrom=subset_chrom,
        sep='\t', header=0,
        usecols=lambda col: col in col_set
    )

    # Read SEQ column
    if fa_file_name is not None:
        if 'SEQ' in df.columns:
            raise RuntimeError(f'Duplicate SEQ sources for BED file "{bed_file_name}": BED contains a SEQ column, and read_variant_table() SEQ from a FASTA file')

        df = df.join(svpoplib.seq.fa_to_series(fa_file_name), on='ID', how='left')

        if np.any(pd.isnull(df['SEQ'])):
            id_missing = list(df.loc[pd.isnull(df['SEQ']), 'ID'])

            raise RuntimeError('Missing {} records in FASTA for sample {}: {}{}'.format(
                len(id_missing), sample_name, ', '.join(id_missing[:3]), '...' if len(id_missing) > 3 else ''
            ))

        if 'SEQ' not in col_list:
            col_list = tuple(list(col_list) + ['SEQ'])

    elif 'SEQ' in df.columns:
        if 'SEQ' not in col_list:
            col_list = tuple(list(col_list) + ['SEQ'])

    # Set defaults for missing columns
    if 'SVLEN' not in df.columns:

        if 'END' not in df.columns or 'SVTYPE' not in df.columns:
            raise RuntimeError(f'Missing SVLEN in {bed_file_name}: Need both SVTYPE and END to set automatically')

        if (np.any(df['SVTYPE'].apply(lambda val: val.upper()) == 'INS')):
            raise RuntimeError(f'Missing SVLEN in {bed_file_name}: Cannot compute for insertions (SVTYPE must not be INS for any record)')

        df['SVLEN'] = df['END'] - df['POS']

    if 'SVTYPE' not in df.columns:
        df['SVTYPE'] = 'RGN'

    # Order and subset columns
    try:
        df = svpoplib.variant.order_variant_columns(df, col_list, subset=True)
    except RuntimeException as ex:
        raise RuntimeError(f'Error checking columns in {bed_file_name}: {ex}')

    # Check SVLEN
    if np.any(df['SVLEN'] < 0):
        raise RuntimeError(f'Negative SVLEN entries in {bed_file_name}')

    # Sort
    df.sort_values(['#CHROM', 'POS', 'SVLEN', 'ID'], inplace=True)

    # Check for unique IDs
    dup_names = [name for name, count in collections.Counter(df['ID']).items() if count > 1]

    if dup_names:
        dup_name_str = ', '.join(dup_names[:3])

        if len(dup_names) > 3:
            dup_name_str += ', ...'

        raise RuntimeError('Found {} IDs with duplicates in sample {}: {} (file {})'.format(
            len(dup_names), sample_name, dup_name_str, bed_file_name
        ))

    # Set index
    df.set_index(df['ID'], inplace=True, drop=False)
    df.index.name = 'INDEX'

    # Return variant DataFrame
    return df


def get_support_table(
        df, df_next,
        threads,
        offset_max, ro_szro_min,
        match_ref, match_alt,
        aligner=None,
        align_match_prop=ALIGN_PARAM_DEFAULT['SCORE-PROP']
    ):
    """
    Get a table describing matched variants between `df` and `df_next` and columns of evidence for support.

    :param df: Set of variants in the accepted set.
    :param df_next: Set of variants to intersect with `df`.
    :param threads: Number of threads to run.
    :param offset_max: Max breakpoint offset (offset is the minimum of start position and end position offsets).
    :param ro_szro_min: Minimum reciprocal-overlap.
    :param match_ref: REF column must match if True.
    :param match_alt: ALT column must match if True.
    :param aligner: Configured aligner for matching sequences.
    :param align_match_prop: Minimum matched base proportion in alignment.

    See `svpoplib.svlenoverlap.nearest_by_svlen_overlap` for a description of the returned columns.

    :return: A table describing variant support between `df` (ID) and `df_next` (TARGET_ID) and supporting evidence
        (OFFSET, RO, SZRO, OFFSZ, MATCH).
    """

    interval_flank = (offset_max if offset_max is not None else 0) + 1

    if df_next.shape[0] > 0:

        df_support_list_chrom = list()

        chrom_list = sorted(set(df['#CHROM']) | set(df_next['#CHROM']))

        for chrom in chrom_list:

            df_chrom = df.loc[df['#CHROM'] == chrom]
            df_next_chrom = df_next.loc[df_next['#CHROM'] == chrom]

            # Split merge, isolate to overlapping intervals before merging.
            # This strategy limits combinatorial explosion merging large sets.

            # Create an interval tree of records to intersect.
            #
            # For each interval, the data element will be a tuple of two sets:
            #   [0]: Set of source variant IDs in the interval.
            #   [1]: Set of target variant IDs in the interval.
            tree = intervaltree.IntervalTree()

            # Add max intervals for each source variant
            for row_index, row in df_chrom.iterrows():
                tree.addi(
                    row['POS'] - interval_flank,
                    (row['END'] if row['SVTYPE'] != 'INS' else row['POS'] + row['SVLEN']) + interval_flank,
                    ({row['ID']}, set())
                )

            # For each target variant in turn, merge all source intervals it intersects. Source and target ID sets
            # in the interval data are merged with the intervals.
            for row_index, row in df_next_chrom.iterrows():

                pos = row['POS']
                end = (row['END'] if row['SVTYPE'] != 'INS' else row['POS'] + row['SVLEN'])

                source_rows = set()
                target_rows = {row['ID']}

                pos_set = set()
                end_set = set()

                # Collapse intersecting intervals
                for interval in tree[pos:end]:
                    pos_set.add(interval.begin)
                    end_set.add(interval.end)

                    source_rows |= interval.data[0]
                    target_rows |= interval.data[1]

                    tree.discard(interval)

                # Add new interval if any
                if source_rows:
                    tree.addi(min(pos_set), max(end_set), (source_rows, target_rows))

            # Create a list of tuples where each element is the data from the interval. Include only intervals
            # with at least one target row.
            record_pair_list = [interval.data for interval in tree if len(interval.data[1]) > 0]

            del tree

            # Report
            print('\t* Split ref {} into {} parts'.format(chrom, len(record_pair_list)))

            # Shortcut if no records
            if len(record_pair_list) > 0:

                # Init merged table list (one for each record pair)
                df_support_list = [None] * len(record_pair_list)

                # Setup jobs
                pool = multiprocessing.Pool(threads)

                kwd_args = {
                    'szro_min': ro_szro_min,
                    'offset_max': offset_max,
                    'priority': ['RO', 'OFFSET', 'SZRO'],
                    'threads': 1,
                    'match_ref': match_ref,
                    'match_alt': match_alt,
                    'aligner': aligner,
                    'align_match_prop': align_match_prop
                }

                # Setup callback handler
                def _apply_parallel_cb_result(record_pair_index, df_support_list):
                    """ Get a function to save results. """

                    def callback_handler(subdf):
                        df_support_list[record_pair_index] = subdf

                    return callback_handler

                def _apply_parallel_cb_error(record_pair_index, df_support_list):
                    """Get an error callback function"""

                    def callback_handler(ex):
                        df_support_list[record_pair_index] = ex

                        print(f'Failed {record_pair_index}: {ex}', file=sys.stderr)
                        traceback.print_tb(ex.__traceback__)
                        sys.stderr.flush()

                        try:
                            print(f'Terminating: {record_pair_index}', file=sys.stderr)
                            sys.stderr.flush()

                            pool.terminate()

                        except Exception as ex:
                            print(f'Caught error while terminating: {record_pair_index}: {ex}', file=sys.stderr)
                            sys.stderr.flush()

                        print(f'Exiting error handler: {record_pair_index}')

                    return callback_handler

                # Submit jobs
                for record_pair_index in range(len(record_pair_list)):

                    try:
                        pool.apply_async(
                            svpoplib.svlenoverlap.nearest_by_svlen_overlap,
                            (
                                df_chrom.loc[
                                    df_chrom['ID'].apply(lambda var_id: var_id in record_pair_list[record_pair_index][0])
                                ],
                                df_next_chrom.loc[
                                    df_next_chrom['ID'].apply(lambda var_id: var_id in record_pair_list[record_pair_index][1])
                                ]
                            ),
                            kwd_args,
                            _apply_parallel_cb_result(record_pair_index, df_support_list),
                            _apply_parallel_cb_error(record_pair_index, df_support_list)
                        )

                    except:
                        pass

                # Wait for jobs
                print('Waiting...')
                sys.stderr.flush()
                sys.stdout.flush()

                pool.close()
                pool.join()
                sys.stderr.flush()
                sys.stdout.flush()

                print('Done Waiting.')
                sys.stderr.flush()
                sys.stdout.flush()

                # Check for exceptions
                for df_support in df_support_list:
                    if issubclass(df_support.__class__, Exception):
                        raise df_support

                # Check for null output
                n_fail = np.sum([val is None for val in df_support_list])

                if n_fail > 0:
                    raise RuntimeError('Failed merging {} of {} record groups'.format(n_fail, len(df_support_list)))

                # Merge supporting dataframes
                df_support = pd.concat(df_support_list, axis=0, sort=False).reset_index(drop=True)

                # Clean up
                del df_support_list

            else:
                df_support = pd.DataFrame(columns=['ID', 'TARGET_ID', 'OFFSET', 'RO', 'SZRO', 'OFFSZ', 'MATCH'])

            # Add to list (one list item per chromosome)
            df_support_list_chrom.append(df_support)

            # Clean up
            del record_pair_list

        # Merge chromosomes and return
        df_support = pd.concat(df_support_list_chrom, axis=0)
        del df_support_list_chrom

    else:
        df_support = svpoplib.svlenoverlap.nearest_by_svlen_overlap(
            df, df_next,
            ro_szro_min,
            offset_max,
            priority=['RO', 'OFFSET', 'SZRO', 'MATCH'],
            threads=threads,
            match_ref=match_ref,
            match_alt=match_alt
        )

    return df_support


def get_support_table_nrid(df, df_next):
    """
    Get an intersect table of exact-match variants by ID.

    :param df: Dataframe.
    :param df_next: Next dataframe.

    :return: Return a support table with exact matches.
    """

    intersect_set = set(df['ID']) & (set(df_next['ID']))

    intersect_list = [id for id in df['ID'] if id in intersect_set]

    intersect_n = len(intersect_list)

    return pd.DataFrame(
        zip(intersect_list, intersect_list, [0] * intersect_n, [1] * intersect_n, [1] * intersect_n, [0] * intersect_n),
        columns=['ID', 'TARGET_ID', 'OFFSET', 'RO', 'SZRO', 'OFFSZ']
    )


def get_support_table_exact(df, df_next, match_seq=None, match_ref=None, match_alt=None):
    """
    Get an intersect table of exact breakpoint matches.

    :param df: Dataframe.
    :param df_next: Next dataframe.
    :param match_seq: Exact match on sequence.

    :return: Return a support table with exact matches.
    """

    # Set columns to sort by
    sort_cols = ['#CHROM', 'POS', 'SVLEN']

    # Set match_ref
    if match_ref is None:
        match_ref = 'REF' in df.columns or 'REF' in df_next.columns

    if match_ref:
        if 'REF' not in df.columns:
            raise RuntimeError('Cannot match REF for exact match intersect: No REF column in dataframe (df)')

        if 'REF' not in df_next.columns:
            raise RuntimeError('Cannot match REF for exact match intersect: No REF column in dataframe (df_next)')

        sort_cols += ['REF']

    # Set match_alt
    if match_alt is None:
        match_alt = 'ALT' in df.columns or 'ALT' in df_next.columns

    if match_alt:
        if 'ALT' not in df.columns:
            raise RuntimeError('Cannot match ALT for exact match intersect: No ALT column in dataframe (df)')

        if 'ALT' not in df_next.columns:
            raise RuntimeError('Cannot match ALT for exact match intersect: No ALT column in dataframe (df_next)')

        sort_cols += ['ALT']

    # Set match_seq
    if match_seq is None:
        match_seq = 'SEQ' in df.columns or 'SEQ' in df_next.columns

    if match_seq:
        if 'SEQ' not in df.columns:
            raise RuntimeError('Cannot match sequences for exact match intersect: No SEQ column in dataframe (df)')

        if 'SEQ' not in df_next.columns:
            raise RuntimeError('Cannot match sequences for exact match intersect: No SEQ column in dataframe (df_next)')

        sort_cols += ['SEQ']

    # Check for missing columns
    missing_1 = [col for col in sort_cols if col not in df.columns]
    missing_2 = [col for col in sort_cols if col not in df_next.columns]

    if missing_1 or missing_2:
        raise RuntimeError('Missing columns for exact merging: df="{}", df_next="{}"'.format(
            ', '.join(missing_1), ', '.join(missing_2)
        ))

    # Sort
    df = df.sort_values(sort_cols)
    df_next = df_next.sort_values(sort_cols)

    # Find exact matches
    index_1 = 0
    index_2 = 0

    max_index_1 = df.shape[0]
    max_index_2 = df_next.shape[0]

    df_match_list = list()

    while (index_1 < max_index_1 and index_2 < max_index_2):

        # CHROM
        if df_next.iloc[index_2]['#CHROM'] < df.iloc[index_1]['#CHROM']:
            index_2 += 1
            continue
        elif df_next.iloc[index_2]['#CHROM'] > df.iloc[index_1]['#CHROM']:
            index_1 += 1
            continue

        # POS
        if df_next.iloc[index_2]['POS'] < df.iloc[index_1]['POS']:
            index_2 += 1
            continue
        elif df_next.iloc[index_2]['POS'] > df.iloc[index_1]['POS']:
            index_1 += 1
            continue

        # SVLEN (END)
        if df_next.iloc[index_2]['SVLEN'] < df.iloc[index_1]['SVLEN']:
            index_2 += 1
            continue
        elif df_next.iloc[index_2]['SVLEN'] > df.iloc[index_1]['SVLEN']:
            index_1 += 1
            continue

        # REF
        if match_ref:
            if df_next.iloc[index_2]['REF'] < df.iloc[index_1]['REF']:
                index_2 += 1
                continue
            elif df_next.iloc[index_2]['REF'] > df.iloc[index_1]['REF']:
                index_1 += 1
                continue

        # ALT
        if match_alt:
            if df_next.iloc[index_2]['ALT'] < df.iloc[index_1]['ALT']:
                index_2 += 1
                continue
            elif df_next.iloc[index_2]['ALT'] > df.iloc[index_1]['ALT']:
                index_1 += 1
                continue

        # SEQ
        if match_seq:
            if df_next.iloc[index_2]['SEQ'] < df.iloc[index_1]['SEQ']:
                index_2 += 1
                continue
            elif df_next.iloc[index_2]['SEQ'] > df.iloc[index_1]['SEQ']:
                index_1 += 1
                continue

        # Found match
        df_match_list.append(pd.Series(
            [
                df.iloc[index_1]['ID'],
                df_next.iloc[index_2]['ID'],
                0, 1, 1, 0, 1.0 if match_seq else np.nan
            ],
            index=['ID', 'TARGET_ID', 'OFFSET', 'RO', 'SZRO', 'OFFSZ', 'MATCH']
        ))

        index_1 += 1
        index_2 += 1

    # Merge match dataframe
    if df_match_list:
        return pd.concat(df_match_list, axis=1).T
    else:
        return pd.DataFrame(
            [],
            columns=['ID', 'TARGET_ID', 'OFFSET', 'RO', 'SZRO', 'OFFSZ', 'MATCH']
        )


# def align_match_prop_dup(seq_a, seq_b, aligner):
#     """
#     Determine if two sequences match by alignment.
#
#     The alignment aligns seq_a to seq_b + seq_b (seq_b is duplicated head-to-tail). This allows sequences to match if
#     they both represent a tandem duplication but a different breakpoint within the duplicated sequence.
#
#     :param seq_a: Sequence (first).
#     :param seq_b: Sequence (second).
#     :param aligner: Pre-configured alignment object.
#
#     :return: Proportion of matched bases in the alignment.
#     """
#
#     max_len = np.max([len(seq_a), len(seq_b)])
#     min_len = np.min([len(seq_a), len(seq_b)])
#
#     return min([
#             np.min([aligner.score_align(seq_a, seq_b + seq_b), min_len * 2]) / (max_len * 2),
#             1.0
#     ])
