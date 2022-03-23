"""
Variant processing and comparison functions.
"""

import collections
import intervaltree
import multiprocessing
import numpy as np
import os
import pandas as pd
import re


def reciprocal_overlap(begin_a, end_a, begin_b, end_b):
    """
    Get reciprocal overlap of two intervals. Intervals are expected to be half-open coordinates (length is end - start).

    :param begin_a: Begin of interval a.
    :param end_a: End of interval a.
    :param begin_b: Begin of interval b.
    :param end_b: End of interval b.

    :return: A value between 0 and 1 if intervals a and b overlap and some negative number if they do not.
    """

    overlap = min(end_a, end_b) - max(begin_a, begin_b)

    if overlap < 0:
        return 0.0

    return min([
        overlap / (end_a - begin_a),
        overlap / (end_b - begin_b)
    ])


def var_nearest(df_a, df_b, ref_alt=False, verbose=False):
    """
    For each variant in `df_a`, get the nearest variant in `df_b`. All `df_a` variants are in the output except those
    where `df_b` has no variant call on the same chromosome.

    :param df_a: Variants to match.
    :param df_b: Variants to match against.
    :param ref_alt: Find distance to nearest variant where "REF" and "ALT" columns match (for SNVs of the same type).
    :param verbose: Print status information.

    :return: A dataframe with columns "ID_A", "ID_B", and "DISTANCE". If a variant from `df_b` is downstream, the
        distance is positive.
    """

    # Check
    if ref_alt:
        if 'REF' not in df_a.columns or 'ALT' not in df_a.columns:
            raise RuntimeError('Missing required column(s) in dataframe A for ref-alt comparisons: REF, ALT')

        if 'REF' not in df_b.columns or 'ALT' not in df_b.columns:
            raise RuntimeError('Missing required column(s) in dataframe B for ref-alt comparisons: REF, ALT')

    # Process each chromosome
    match_list = list()

    for chrom in sorted(set(df_a['#CHROM'])):
        if verbose:
            print('Chrom: ' + chrom)

        # Subset by chromosome
        df_b_chrom = df_b.loc[df_b['#CHROM'] == chrom]

        if df_b_chrom.shape[0] == 0:
            continue

        df_a_chrom = df_a.loc[df_a['#CHROM'] == chrom]

        # Get a set of REF-ALT tuples from df_a (if ref_alt)
        if ref_alt:
            ref_alt_set = set(df_a_chrom.loc[:, ['REF', 'ALT']].apply(tuple, axis=1))
        else:
            ref_alt_set = {(None, None)}

        # Process each subset for this chromosome
        for ref, alt in ref_alt_set:

            # Separate on REF/ALT (if ref_alt is True)
            if ref is not None and alt is not None:
                df_a_sub = df_a_chrom.loc[(df_a_chrom['REF'] == ref) & (df_a_chrom['ALT'] == alt)]
                df_b_sub = df_b_chrom.loc[(df_b_chrom['REF'] == ref) & (df_b_chrom['ALT'] == alt)]
            else:
                df_a_sub = df_a_chrom
                df_b_sub = df_b_chrom

            if df_a_sub.shape[0] == 0 or df_b_sub.shape[0] == 0:
                continue

            # Get arrays from b for comparisons
            pos_array = np.array(df_b_sub['POS'])
            end_array = np.array(df_b_sub['END'])
            id_array = np.array(df_b_sub['ID'])

            # Process each record on chromosome
            for index, row in df_a_sub.iterrows():

                min_pos_index = np.argmin(np.abs(pos_array - row['POS']))
                min_end_index = np.argmin(np.abs(end_array - row['END']))

                min_pos = row['POS'] - pos_array[min_pos_index]
                min_end = row['END'] - end_array[min_end_index]

                # Make intersect record
                if np.abs(min_pos) < np.abs(min_end):
                    match_list.append(pd.Series(
                        [
                            row['ID'],
                            id_array[min_pos_index],
                            min_pos,
                        ],
                        index=['ID_A', 'ID_B', 'DISTANCE']
                    ))

                else:
                    match_list.append(pd.Series(
                        [
                            row['ID'],
                            id_array[min_end_index],
                            min_end,
                        ],
                        index=['ID_A', 'ID_B', 'DISTANCE']
                    ))

    # Return merged dataframe
    return pd.concat(match_list, axis=1).T


def nr_interval_merge(df_chr, overlap=0.5):
    """
    Reduce a dataframe to non-redundant intervals based on reciprocal overlap. All records in the dataframe must be
    on the same chromosome.

    :param df_chr: DataFrame of one chromosome.
    :param overlap: Reciprocal overlap (0, 1].

    :return: Dataframe subset using the first record in a unique interval.
    """

    index_list = list()  # Dataframe indices to return

    interval_tree = intervaltree.IntervalTree()  # Tree of intervals

    # Iterate rows
    for index, row in df_chr.iterrows():
        ri_match = False

        # Find matches
        for interval in interval_tree[row['POS']:row['END']]:
            if reciprocal_overlap(row['POS'], row['END'], interval.begin, interval.end) >= 0.50:
                ri_match = True
                break

        # Append to non-redundant records if no match
        if not ri_match:
            index_list.append(index)

        # All records are added to the tree
        interval_tree[row['POS']:row['END']] = True

    return df_chr.loc[index_list]


def order_variant_columns(
        df, head_cols=('#CHROM', 'POS', 'END', 'ID', 'SVTYPE', 'SVLEN'), tail_cols=None, allow_missing=False, subset=False
):
    """
    Rearrange columns with a set list first (in defined order of `head_cols`) and leave the remaining columns
    in the order they were found.

    :param df: Data frame.
    :param head_cols: Columns to move to the first columns. Set variant BED order by default.
    :param tail_cols: Columns to move to the end. May be set to `None`.
    :param allow_missing: Do not throw an error if the dataframe is missing one or more columns.
    :param subset: If True, subset to defined columns and drop all others.

    :return: Data frame with rearranged columns.
    """

    # Check head columns
    if head_cols is not None:
        head_cols = list(head_cols)
    else:
        head_cols = list()

    if not allow_missing:
        for col in head_cols:
            if col not in df.columns:
                raise RuntimeError('Missing head column in variant file: {}'.format(col))
    else:
        head_cols = [col for col in head_cols if col in df.columns]

    # Check tail columns
    if tail_cols is not None:
        tail_cols = list(tail_cols)
    else:
        tail_cols = list()

    if not allow_missing:
        for col in tail_cols:
            if col not in df.columns:
                raise RuntimeError('Missing tail column in variant file: {}'.format(col))
    else:
        tail_cols = [col for col in tail_cols if col in df.columns]

    # Give precedence for head columns if a col is head and tail
    tail_cols = [col for col in tail_cols if col not in head_cols]

    # Check for empty column sets
    if len(head_cols) == 0 and len(tail_cols) == 0:
        raise RuntimeError('No head or tail columns to sort (after filtering for missing columns if allow_missing=True)')

    # Define middle columns
    head_tail_set = set(head_cols).union(set(tail_cols))

    if subset:
        mid_cols = []
    else:
        mid_cols = [col for col in df.columns if col not in head_tail_set]

    # Arrange with head columns first. Leave remaining columns in order
    return df.loc[:, head_cols + mid_cols + tail_cols]


def get_variant_id(df, apply_version=True):
    """
    Get variant IDs using '#CHROM', 'POS', 'SVTYPE', and 'SVLEN' columns.

    :param df: Dataframe.
    :param apply_version: Version ID (add "." and a number for duplicated IDs). SV-Pop does not allow duplicate IDs, so
        this should be explicitly turned on unless duplicates are checked and handled explicitly. If there are no
        duplicate IDs before versioning, this option has no effect on the output ("." in only added if necessary).

    :return: A Series of variant IDs for `df`.
    """

    id_col = df.apply(get_variant_id_from_row, axis=1)

    if apply_version:
        id_col = version_id(id_col)

    return id_col


def get_variant_id_from_row(row):
    """
    Get variant ID for one row.

    :param row: Variant row.

    :return: Variant ID.
    """

    if row['SVTYPE'] != 'SNV':
        return '{}-{}-{}-{}'.format(
            row['#CHROM'], row['POS'] + 1, row['SVTYPE'], row['SVLEN']
        )

    else:
        return '{}-{}-{}-{}-{}'.format(
            row['#CHROM'], row['POS'] + 1, row['SVTYPE'], row['REF'].upper(), row['ALT'].upper()
        )


def vcf_fields_to_seq(row, pos_row='POS', ref_row='REF', alt_row='ALT'):
    """
    Get call for one VCF record and one sample.

    Example command-line to generate input table from a VCF:

    bcftools query -H -f"%CHROM\t%POS\t%REF\t%ALT\t%QUAL\t%FILTER[\t%GT][\t%GQ][\t%DP][\t%AD]\n" INPUT.vcf.gz | gzip > OUTPUT.vcf.tab.gz

    :param row: Table row.
    :param pos_row: Row name for the variant position (POS).
    :param ref_row: Row name for the reference sequence (REF).
    :param alt_row: Row name for the alternate sequence (ALT).

    :return: Standard BED format of variant calls with "POS", "END", "VARTYPE", "SVTYPE", "SVLEN", "SEQ", "REF".
    """

    pos = row[pos_row]
    ref = row[ref_row].upper().strip()
    alt = row[alt_row].upper().strip()

    # This function does not handle multiple alleles or missing ALTs (one variant per record)
    if ',' in alt:
        raise RuntimeError(f'Multiple alleles in ALT, separate before calling vcf_fields_to_seq(): "{alt}"')

    if alt == '.':
        raise RuntimeError('Missing ALT in record')

    # Handle symbolic SV variants
    if alt[0] == '<' and alt[-1] == '>':

        # Get type
        svtype = alt[1:-1].split(':', 1)[0]

        if svtype not in {'INS', 'DEL', 'INV', 'DUP', 'CNV'}:
            raise RuntimeError('Unrecognized symbolic variant type: {}: Row {}'.format(svtype, row.name))

        # Get length
        svlen = None

        if 'SVLEN' in row:
            try:
                svlen = abs(int(row['SVLEN']))
            except:
                svlen = None
        else:
            svlen = None

        if svlen is None:
            if 'END' not in row:
                raise RuntimeError('Missing or 0-length SVLEN and no END for symbolic SV: Row {}'.format(row.name))

            try:
                svlen = abs(int(row['END'])) - pos
            except:
                raise RuntimeError('Variant has no SVLEN and END is not an integer: {}: Row {}'.format(row['END'], row.name))

        # Set variant type
        vartype = 'INDEL' if svlen < 50 else 'SV'

        # Set end
        if svtype == 'INS':
            end = pos + 1
        else:
            end = pos + svlen

        # Sequence
        seq = row['SEQ'] if 'SEQ' in row else np.nan

    elif alt == '.':
        vartype = 'NONE'
        svtype = 'NONE'
        seq = np.nan
        ref = np.nan
        end = pos + 1
        svlen = 0

    elif '[' in alt or ']' in alt or '.' in alt:
        vartype = 'BND'
        svtype = 'BND'
        seq = np.nan
        ref = np.nan
        end = pos + 1
        svlen = 0

    elif re.match('^[a-zA-Z]+$', alt) and re.match('^[a-zA-Z]+$', ref):

        min_len = min(len(ref), len(alt))

        trim_left = 0

        # Trim left
        while min_len and ref[0] == alt[0]:
            ref = ref[1:]
            alt = alt[1:]
            trim_left += 1
            min_len -= 1

        # Trim right
        while min_len and ref[-1] == alt[-1]:
            ref = ref[:-1]
            alt = alt[:-1]
            min_len -= 1

        # Check variant type
        if ref == '' and alt != '':
            svtype = 'INS'
            seq = alt
            svlen = len(seq)
            vartype = 'INDEL' if svlen < 50 else 'SV'
            pos = pos + trim_left - 1
            end = pos + 1

        elif ref != '' and alt == '':
            svtype = 'DEL'
            seq = ref
            svlen = len(seq)
            vartype = 'INDEL' if svlen < 50 else 'SV'
            pos = pos + trim_left - 1
            end = pos + svlen
            ref = ''

        elif len(ref) == 1 and len(alt) == 1:
            vartype = 'SNV'
            svtype = 'SNV'
            seq = alt
            svlen = 1
            pos = pos + trim_left - 1
            end = pos + 1

        else:
            vartype = 'SUB'
            svtype = 'SUB'
            seq = alt
            svlen = len(seq)
            pos = pos + trim_left - 1
            end = pos + svlen

    else:
        raise RuntimeError(f'Unknown variant type: REF="{ref}", ALT="{alt}')

    # Return with AC
    if 'GT' in row.index:
        ac = gt_to_ac(row['GT'], no_call=-1, no_call_strict=False)
    else:
        ac = np.nan

    return pd.Series(
        [pos, end, vartype, svtype, svlen, ac, seq, ref],
        index=['POS', 'END', 'VARTYPE', 'SVTYPE', 'SVLEN', 'AC', 'SEQ', 'REF']
    )


def gt_to_ac(gt, no_call=-1, no_call_strict=False):
    """
    Convert a genotype string to an allele count.

    :param gt: Genotype string. Examples are "0/1", "0|1", "./.", ".|1".
    :param no_call: If all alleles are no-call ("."), then return this value.
    :param no_call_strict: If any alleles are no-call, return `no_call`.

    :return: Genotype count.
    """
    ac = 0

    gt_list = re.split('[/|]', gt)

    # Handle all no-call
    if '.' in gt_list:
        if set(gt_list) == {'.'} or no_call_strict:
            return no_call

    # Get AC
    for gt_allele in gt_list:
        if gt_allele != '.' and int(gt_allele) > 0:
            ac += 1

    return ac


def get_filter_bed(filter_name, ucsc_ref_name, config, svpop_dir):
    """
    Get a BED file defining a filter. Searches config['filter'] for the filter name (key) and path (value). If not
    found, the default is "files/filter/{ucsc_ref_name}/{filter}.bed" within SV-Pop pipeline directory.

    If wildcard "ucsc_ref_name" is in the filter path, then "ucsc_ref_name" is parsed into it.

    :param filter_name: Fliter name.
    :param ucsc_ref_name: Name of the UCSC reference (e.g. "hg38").
    :param config: SV-Pop config.
    :param svpop_dir: SV-Pop pipeline directory.

    :return: Filter path.
    """

    # Get path
    filter_path = config.get('filter', dict()).get(filter_name, None)

    if filter_path is None:
        filter_path = os.path.join(
            svpop_dir, 'files/filter/{ucsc_ref_name}/{filter}.bed'.format(
                ucsc_ref_name=ucsc_ref_name,
                filter=filter_name
            )
        )

    elif '{ucsc_ref_name}' in filter_path:
        filter_path = filter_path.format(
            ucsc_ref_name=ucsc_ref_name,
        )

    # Check path
    if not os.path.isfile(os.path.join(svpop_dir, filter_path)):
        raise RuntimeError('Cannot find filter {}: {}'.format(filter_name, filter_path))

    # Return
    return filter_path


def qual_to_filter(row, min_qv=30.0):
    """
    Use VCF "QUAL" field to fill "FILTER".

    :return: "PASS" if "QUAL" is numeric and greater than or equal to `min_qv`, "FAIL" if "PASS" if "QUAL" is numeric
        and less than `min_qv`, and "." if "QUAL" is not numeric.
    """

    if row['FILTER'] == '.':
        try:
            return 'PASS' if float(row['QUAL']) >= min_qv else 'FAIL'
        except ValueError:
            return '.'
    else:
        return row['FILTER']


def left_homology(pos_tig, seq_tig, seq_sv):
    """
    Duplicated from from PAV (https://github.com/EichlerLab/pav).

    Determine the number of perfect-homology bp upstream of an SV/indel using the SV/indel sequence (seq_sv), a contig
    or reference sequence (seq_tig) and the position of the first base upstream of the SV/indel (pos_tig) in 0-based
    coordinates. Both the contig and SV/indel sequence must be in the same orientation (reverse-complement if needed).
    Generally, the SV/indel sequence is in reference orientation and the contig sequence is the reference or an
    aligned contig in reference orientation (reverse-complemented if needed to get to the + strand).

    This function traverses from `pos_tig` to upstream bases in `seq_tig` using bases from the end of `seq_sv` until
    a mismatch between `seq_sv` and `seq_tig` is found. Search will wrap through `seq_sv` if homology is longer than
    the SV/indel.

    WARNING: This function assumes upper-case for the sequences. Differing case will break the homology search. If any
    sequence is None, 0 is returned.

    :param pos_tig: Contig/reference position (0-based) in reference orientation (may have been reverse-complemented by an
        alignment) where the homology search begins.
    :param seq_tig: Contig sequence as an upper-case string and in reference orientation (may have been reverse-
        complemented by the alignment).
    :param seq_sv: SV/indel sequence as an upper-case string.

    :return: Number of perfect-homology bases between `seq_sv` and `seq_tig` immediately upstream of `pos_tig`. If any
        of the sequneces are None, 0 is returned.
    """

    if seq_sv is None or seq_tig is None:
        return 0

    svlen = len(seq_sv)

    hom_len = 0

    while hom_len <= pos_tig:  # Do not shift off the edge of a contig.
        seq_tig_base = seq_tig[pos_tig - hom_len]

        # Do not match ambiguous bases
        if seq_tig_base not in {'A', 'C', 'G', 'T'}:
            break

        # Match the SV sequence (dowstream SV sequence with upstream reference/contig)
        if seq_sv[-((hom_len + 1) % svlen)] != seq_tig_base:
            # Circular index through seq in reverse from last base to the first, then back to the first
            # if it wraps around. If the downstream end of the SV/indel matches the reference upstream of
            # the SV/indel, shift left. For tandem repeats where the SV was placed in the middle of a
            # repeat array, shift through multiple perfect copies (% oplen loops through seq).
            break

        hom_len += 1

    # Return shifted amount
    return hom_len


def right_homology(pos_tig, seq_tig, seq_sv):
    """
    Duplicated from from PAV (https://github.com/EichlerLab/pav).

    Determine the number of perfect-homology bp downstream of an SV/indel using the SV/indel sequence (seq_sv), a contig
    or reference sequence (seq_tig) and the position of the first base downstream of the SV/indel (pos_tig) in 0-based
    coordinates. Both the contig and SV/indel sequence must be in the same orientation (reverse-complement if needed).
    Generally, the SV/indel sequence is in reference orientation and the contig sequence is the reference or an
    aligned contig in reference orientation (reverse-complemented if needed to get to the + strand).

    This function traverses from `pos_tig` to downstream bases in `seq_tig` using bases from the beginning of `seq_sv` until
    a mismatch between `seq_sv` and `seq_tig` is found. Search will wrap through `seq_sv` if homology is longer than
    the SV/indel.

    WARNING: This function assumes upper-case for the sequences. Differing case will break the homology search. If any
    sequence is None, 0 is returned.

    :param pos_tig: Contig/reference position (0-based) in reference orientation (may have been reverse-complemented by an
        alignment) where the homology search begins.
    :param seq_tig: Contig sequence as an upper-case string and in reference orientation (may have been reverse-
        complemented by the alignment).
    :param seq_sv: SV/indel sequence as an upper-case string.

    :return: Number of perfect-homology bases between `seq_sv` and `seq_tig` immediately downstream of `pos_tig`. If any
        of the sequences are None, 0 is returned.
    """

    if seq_sv is None or seq_tig is None:
        return 0

    svlen = len(seq_sv)
    tig_len = len(seq_tig)

    hom_len = 0
    pos_tig_limit = tig_len - pos_tig

    while hom_len < pos_tig_limit:  # Do not shift off the edge of a contig.
        seq_tig_base = seq_tig[pos_tig + hom_len]

        # Do not match ambiguous bases
        if seq_tig_base not in {'A', 'C', 'G', 'T'}:
            break

        # Match the SV sequence (dowstream SV sequence with upstream reference/contig)
        if seq_sv[hom_len % svlen] != seq_tig_base:
            # Circular index through seq in reverse from last base to the first, then back to the first
            # if it wraps around. If the downstream end of the SV/indel matches the reference upstream of
            # the SV/indel, shift left. For tandem repeats where the SV was placed in the middle of a
            # repeat array, shift through multiple perfect copies (% oplen loops through seq).
            break

        hom_len += 1

    # Return shifted amount
    return hom_len


def version_id(id_col, existing_id_set=None):
    """
    Take a column of IDs (Pandas Series object, `id_col`) and transform all duplicate IDs by appending "." and an
    integer so that no duplicate IDs remain.

    Example: If "chr1-1000-INS-10" has no duplicates, it will remain "chr1-1000-INS-10" in the output column. If it
    appears 3 times, then they will be named "chr1-1000-INS-10.1", "chr1-1000-INS-10.2", and "chr1-1000-INS-10.3".

    If an ID is duplicated and already versioned, the first appearance of the versioned ID will remain unchanged and the
    version will be incremented for subsequent appearances. If upon incrementing the name conflicts with another variant
    that was already versioned (e.g. a ".2" version already exists in the callset), the version will be incremented
    until it does not collide with any variant IDs.

    :param id_col: ID column as a Pandas Series object.
    :param existing_id_set: A set of existing variant IDs that must also be avoided. If any IDs in id_col match these
        IDs, they are altered as if the variant intersects another ID in id_col.

    :return: `id_col` unchanged if there are no duplicate IDs, or a new copy of `id_col` with IDs de-duplicated and
        versioned.
    """

    # Get counts
    id_count = collections.Counter()

    if existing_id_set is not None:
        id_count.update(existing_id_set)

    id_count.update(id_col)

    dup_set = {val for val, count in id_count.items() if count > 1}

    if len(dup_set) == 0:
        return id_col

    # Create a map: old name to new name
    id_col = id_col.copy()
    id_set = set(id_col) - dup_set

    if existing_id_set is not None:
        id_set |= existing_id_set

    for index in range(id_col.shape[0]):
        name = id_col.iloc[index]

        if name in dup_set:

            # Get current variant version (everything after "." if present, 1 by default)
            tok = name.rsplit('.', 1)
            if len(tok) == 1:
                name_version = 1

            else:
                try:
                    name_version = int(tok[1]) + 1
                except ValueError:
                    raise RuntimeError(f'Error de-duplicating variant ID field: Split "{name}" on "." and expected to find an integer at the end')

            # Find unique name
            new_name = '.'.join([tok[0], str(name_version)])

            while new_name in id_set:
                name_version += 1
                new_name = '.'.join([tok[0], str(name_version)])

            # Add to map
            id_col.iloc[index] = new_name
            id_set.add(new_name)

    # Append new variants
    return id_col


def check_unique_ids(df, message=''):
    """
    Check for unique IDs in a dataframe or an ID row. If IDs are not unique, throw a runtime error.

    :param df: Dataframe with an 'ID' column (pandas.DataFrame) or an ID column (pandas.Series).
    :param message: Prefix the exception message with this value if defined. (e.g. "{message}: Found X duplicate...").

    :return: No return value, throws an exception on failure, no effect if IDs are unique.
    """

    # Get ID row
    if issubclass(df.__class__, pd.DataFrame):
        if 'ID' not in df.columns:
            raise RuntimeError('Cannot check for unique IDs is DataFrame: No ID column')

        id_row = df['ID']

    elif issubclass(df.__class__, pd.Series):
        id_row = df

    else:
        raise RuntimeError(f'Unrecognized data type for check_unique_ids(): Expected Pandas DataFrame or Series: {df.__class__}')

    # Check for unique IDs
    if len(set(id_row)) < id_row.shape[0]:
        dup_id_set = [val for val, count in collections.Counter(id_row).items() if count > 1]

        if message is None:
            message = ''
        else:
            message = message.strip()

        raise RuntimeError(
            '{}Found {} duplicate variant IDs: {}{}'.format(
                message + ': ' if message else '',
                len(dup_id_set),
                ', '.join(dup_id_set[:3]),
                '...' if len(dup_id_set) > 3 else ''
            )
        )
