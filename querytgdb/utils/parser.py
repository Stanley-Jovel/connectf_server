import logging
import re
from collections import deque
from functools import partial
from operator import itemgetter
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from uuid import uuid4

import numpy as np
import pandas as pd
import pyparsing as pp
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db.models import Q

from querytgdb.models import Analysis, Annotation, EdgeData, EdgeType, Interaction, Regulation
from querytgdb.utils import annotations
from ..utils import read_cached_result

__all__ = ['get_query_result', 'expand_ref_ids']

logger = logging.getLogger(__name__)


class QueryError(ValueError):
    pass


class TargetFrame(pd.DataFrame):
    _metadata = ['include', 'filter_string']

    @property
    def _constructor(self):
        return TargetFrame

    @property
    def _constructor_sliced(self):
        return TargetSeries

    def __init__(self, *args, include=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.include = include
        self.filter_string = ''


class TargetSeries(pd.Series):
    @property
    def _constructor(self):
        return TargetSeries

    @property
    def _constructor_expanddim(self):
        return TargetFrame


name = pp.Word(pp.pyparsing_unicode.alphanums + '-_.:')

point = pp.Literal('.')
e = pp.CaselessLiteral('e')
number = pp.Word(pp.nums)
plusorminus = pp.Literal('+') | pp.Literal('-')
integer = pp.Combine(pp.Optional(plusorminus) + number)

floatnum = pp.Combine(integer + pp.Optional(point + pp.Optional(number)) + pp.Optional(e + integer)).setParseAction(
    lambda toks: float(toks[0]))

and_ = pp.CaselessKeyword('and')
or_ = pp.CaselessKeyword('or')

opers = [
    (pp.CaselessKeyword("not"), 1, pp.opAssoc.RIGHT),
    (and_, 2, pp.opAssoc.LEFT),
    (or_, 2, pp.opAssoc.LEFT)
]

quoted_name = pp.QuotedString('"', escChar='\\') | pp.QuotedString("'", escChar='\\')

modname = pp.Group((name | quoted_name)('key') + pp.oneOf('< = > >= <= !=')('oper') + (name | quoted_name)('value'))(
    'mod')

modifier = pp.Group(pp.Suppress('[') + pp.infixNotation(modname, opers) + pp.Suppress(']'))('modifier')

gene = name('gene_name')

expr = pp.infixNotation(gene, [(modifier, 1, pp.opAssoc.LEFT)] + opers)('query')


def is_name(key: str, item: Union[pp.ParseResults, Any]) -> bool:
    try:
        return item.getName() == key
    except AttributeError:
        return False


is_modifier = partial(is_name, 'modifier')
is_mod = partial(is_name, 'mod')

interactions = Interaction.objects.all()


def mod_to_str(curr: pp.ParseResults) -> str:
    if isinstance(curr, str):
        return curr
    else:
        return ' '.join(map(mod_to_str, curr))


def query_metadata(df: TargetFrame, key: str, value: str) -> pd.DataFrame:
    ref_ids = Analysis.objects.filter(
        (Q(analysisdata__key__name__iexact=key) & Q(analysisdata__value__iexact=value))
    ).values_list('id', flat=True)

    mask = pd.DataFrame(True, columns=df.columns, index=df.index)

    mask.loc[:, ~df.columns.get_level_values(1).isin(ref_ids)] = False

    return mask


def apply_comp_mod(df: TargetFrame, key: str, oper: str, value: Union[float, str]) -> pd.DataFrame:
    """
    apply Pvalue and Log2FC (fold change)
    """
    value = float(value)
    mask = pd.DataFrame(True, columns=df.columns, index=df.index)

    try:
        if oper == '=':
            return mask.where(df[(*df.name, key)] == value, False)
        elif oper == '>=':
            return mask.where(df[(*df.name, key)] >= value, False)
        elif oper == '<=':
            return mask.where(df[(*df.name, key)] <= value, False)
        elif oper == '>':
            return mask.where(df[(*df.name, key)] > value, False)
        elif oper == '<':
            return mask.where(df[(*df.name, key)] < value, False)
        elif oper == '!=':
            return mask.where(df[(*df.name, key)] != value, False)
        else:
            raise ValueError('invalid operator: {}'.format(oper))
    except KeyError:
        mask.loc[:, :] = False
        return mask


def apply_search_column(df: TargetFrame, key, value) -> pd.DataFrame:
    mask = pd.DataFrame(True, columns=df.columns, index=df.index)

    try:
        return mask.where(df[(*df.name, key)].str.contains(value, case=False, regex=False), False)
    except KeyError:
        mask.loc[:, :] = False
        return mask


COL_TRANSLATE = {
    'PVALUE': 'Pvalue',
    'FC': 'Log2FC',
    'ADDITIONAL_EDGE': 'ADD_EDGES'
}


def apply_has_column(df: TargetFrame, value) -> pd.DataFrame:
    try:
        value = COL_TRANSLATE[value]
    except KeyError:
        pass

    if (*df.name, value) in df:
        return pd.DataFrame(True, columns=df.columns, index=df.index)

    return pd.DataFrame(False, columns=df.columns, index=df.index)


def apply_has_add_edges(df: TargetFrame, analyses, anno_ids, value) -> pd.DataFrame:
    mask = pd.DataFrame(True, columns=df.columns, index=df.index)
    try:
        edge_type = EdgeType.objects.get(name__iexact=value)
        target_ids = set(EdgeData.objects.filter(
            type=edge_type,
            tf_id=analyses.get(pk=df.name[1]).tf_id
        ).values_list('target_id', flat=True).iterator())

        target_ids &= set(anno_ids.loc[df.index[df.notna().any(axis=1)]].unique())

        mask.loc[~df.index.isin(anno_ids.index[anno_ids.isin(target_ids)]), :] = False
    except (ObjectDoesNotExist, MultipleObjectsReturned):
        mask.loc[:, :] = False

    return mask


def get_mod(df: TargetFrame, query: Union[pp.ParseResults, pd.DataFrame]) -> pd.DataFrame:
    """
    Get ref_id from modifier to filter TF dataframe

    Careful not to modify original df
    """
    if isinstance(query, pp.ParseResults):
        if 'key' not in query:
            it = iter(query)
            stack = deque()

            try:
                while True:
                    curr = next(it)
                    if curr in ('and', 'or'):
                        prec, succ = get_mod(df, stack.pop()), get_mod(df, next(it))
                        if curr == 'and':
                            stack.append(prec & succ)
                        else:
                            stack.append(prec | succ)
                    elif curr == 'not':
                        succ = get_mod(df, next(it))
                        stack.append(~succ)
                    else:
                        stack.append(curr)
            except StopIteration:
                return get_mod(df, stack.pop())
        else:
            key = query['key']
            oper = query['oper']
            value = query['value']

            if re.match(r'^pvalue$', key, flags=re.I):
                return df.groupby(level=[0, 1], axis=1).apply(apply_comp_mod, key='Pvalue', oper=oper, value=value)
            elif re.match(r'^fc$', key, flags=re.I):
                return df.groupby(level=[0, 1], axis=1).apply(apply_comp_mod, key='Log2FC', oper=oper, value=value)
            elif re.match(r'^additional_edge$', key, flags=re.I):
                analyses = Analysis.objects.filter(pk__in=df.columns.get_level_values(1)).prefetch_related('tf')
                anno_ids = annotations().loc[df.index, 'id']
                return df.groupby(level=[0, 1], axis=1).apply(apply_has_add_edges,
                                                              analyses=analyses,
                                                              anno_ids=anno_ids,
                                                              value=value)
            elif re.match(r'^has_column$', key, flags=re.I):
                value = value.upper()
                return df.groupby(level=[0, 1], axis=1).apply(apply_has_column, value=value)
            else:
                return query_metadata(df, key, value)
    return query


def add_edges(df: pd.DataFrame, edges: List[str]) -> pd.DataFrame:
    """
    Add additional edge data of query to result dataframe
    :param df:
    :param edges:
    :return:
    """
    anno = annotations()['id'].reset_index()

    edge_types = pd.DataFrame(
        EdgeType.objects.filter(name__in=edges).values_list('id', 'name', 'directional').iterator(),
        columns=['edge_id', 'edge', 'directional'])

    try:
        tf_ids = anno.loc[anno['TARGET'].isin(df['TF'].unique()), 'id']
    except KeyError:
        tf_ids = Annotation.objects.filter(analysis__in=df['ANALYSIS'].unique()).values_list('pk', flat=True)

    target_ids = anno.loc[anno['TARGET'].isin(df['TARGET'].unique()), 'id']

    edge_data = pd.DataFrame(
        EdgeData.objects.filter(
            tf_id__in=tf_ids
        ).values_list('tf_id', 'target_id', 'type_id').iterator(),
        columns=['source', 'target', 'edge_id']
    )

    edge_data = edge_data.loc[edge_data['target'].isin(target_ids), :]

    edge_data = (edge_data
                 .merge(edge_types[['edge_id', 'edge']], on='edge_id')
                 .drop('edge_id', axis=1)
                 .set_index(['source', 'target']))

    if not edge_data.empty:
        edge_data = pd.concat(map(itemgetter(1), edge_data.groupby('edge')), axis=1)

        row_num, col_num = edge_data.shape

        if col_num > 1:
            edge_data = edge_data.iloc[:, 0].str.cat(
                map(itemgetter(1), edge_data.iloc[:, 1:].iteritems()),
                sep=',', na_rep='', join='inner').str.strip(',')
        else:
            edge_data = edge_data.fillna('')

        edge_data = edge_data.reset_index()

        edge_data = edge_data.merge(anno, left_on='source', right_on='id').merge(anno, left_on='target',
                                                                                 right_on='id')
        edge_data = edge_data[['TARGET_x', 'TARGET_y', 'edge']]
        edge_data.columns = ['TF', 'TARGET', 'ADD_EDGES']

        if 'TF' in df:
            return df.merge(edge_data, on=['TF', 'TARGET'], how='left')
        return df.merge(edge_data.drop('TF', axis=1), on='TARGET', how='left')

    raise ValueError("No Edge Data")


def get_tf_data(query: str,
                edges: Optional[List[str]] = None,
                tf_filter_list: Optional[pd.Series] = None,
                target_filter_list: Optional[pd.Series] = None) -> TargetFrame:
    """
    Get data for single TF
    :param query:
    :param edges:
    :param tf_filter_list:
    :param target_filter_list:
    :return:
    """
    if (tf_filter_list is not None and tf_filter_list.str.contains(rf'^{re.escape(query)}$', flags=re.I).any()) \
            or tf_filter_list is None:
        analyses = Analysis.objects.filter(tf__gene_id__iexact=query)

        df = TargetFrame(
            interactions.filter(analysis__in=analyses).values_list(
                'target__gene_id', 'analysis_id').iterator(),
            columns=['TARGET', 'ANALYSIS'])
        if target_filter_list is not None:
            df = df[df['TARGET'].str.upper().isin(target_filter_list.str.upper())]
    else:
        analyses = []
        df = TargetFrame(columns=['TARGET', 'ANALYSIS'])

    if not df.empty:
        reg = TargetFrame(
            Regulation.objects.filter(analysis__in=analyses).values_list(
                'analysis_id', 'target__gene_id', 'p_value', 'foldchange').iterator(),
            columns=['ANALYSIS', 'TARGET', 'Pvalue', 'Log2FC'])

        df.insert(2, 'EDGE', '+')

        if not reg.empty:
            # reg['Log2FC'] = reg['Log2FC'].fillna(np.finfo(reg['Log2FC'].dtype).max)
            df = df.merge(reg, on=['ANALYSIS', 'TARGET'], how='left')
            df.loc[df['ANALYSIS'].isin(reg['ANALYSIS']), 'EDGE'] = np.nan

        if edges:
            try:
                df = add_edges(df, edges)
            except ValueError:
                pass

        df = (df.pivot(index='TARGET', columns='ANALYSIS')
              .swaplevel(0, 1, axis=1)
              .sort_index(axis=1, level=0, sort_remaining=False)
              .dropna(axis=1, how='all'))
    else:
        df = TargetFrame(columns=[(np.nan, 'EDGE')])

    df.columns = pd.MultiIndex.from_tuples((query, *c) for c in df.columns)
    df.filter_string += query

    return df


def get_all_tf(query: str,
               edges: Optional[List[str]] = None,
               tf_filter_list: Optional[pd.Series] = None,
               target_filter_list: Optional[pd.Series] = None) -> TargetFrame:
    """
    Get data for all TFs at once
    :param query:
    :param edges:
    :param tf_filter_list:
    :param target_filter_list:
    :return:
    """
    qs = Interaction.objects.values_list('target__gene_id', 'analysis_id')

    # Additional restrictions here
    if query == "multitype":
        a = pd.DataFrame(Analysis.objects.filter(
            analysisdata__key__name__iexact="EXPERIMENT_TYPE"
        ).values_list('id', 'tf_id', 'analysisdata__value', named=True).iterator())

        a = a.groupby('tf_id').filter(lambda x: x['analysisdata__value'].nunique() > 1)

        qs = qs.filter(analysis_id__in=a['id'])

    if tf_filter_list is not None:
        qs = qs.filter(analysis__tf__gene_id__in=tf_filter_list)

    df = TargetFrame(qs.iterator(), columns=['TARGET', 'ANALYSIS'])

    if target_filter_list is not None:
        df = df[df['TARGET'].str.upper().isin(target_filter_list.str.upper())]

    analyses = TargetFrame(
        Analysis.objects.values_list('id', 'tf__gene_id').iterator(),
        columns=['ANALYSIS', 'TF']
    )

    df = df.merge(analyses, on='ANALYSIS')

    reg = TargetFrame(
        Regulation.objects.values_list(
            'analysis_id', 'target__gene_id', 'p_value', 'foldchange').iterator(),
        columns=['ANALYSIS', 'TARGET', 'Pvalue', 'Log2FC'])

    df = df.merge(reg, on=['ANALYSIS', 'TARGET'], how='left')

    if df.empty:
        raise ValueError("No data in database.")

    no_fc = df.groupby(by='ANALYSIS').apply(lambda x: pd.isna(x['Log2FC']).all())

    if edges:
        try:
            df = add_edges(df, edges)
        except ValueError:
            pass

    df.insert(3, 'EDGE', np.nan)
    df['EDGE'] = df['EDGE'].mask(df['ANALYSIS'].isin(no_fc.index[no_fc]), '+')

    df = (df.set_index(['TF', 'ANALYSIS', 'TARGET'])
          .unstack(level=[0, 1])
          .reorder_levels([1, 2, 0], axis=1)
          .sort_index(axis=1, level=[0, 1], sort_remaining=False)
          .dropna(how='all', axis=1))

    df.filter_string += query

    # additional restrictions here as well
    if query == 'andalltfs':
        df = df[df.loc[:, (slice(None), slice(None), ['EDGE', 'Log2FC'])].notna().all(axis=1)]

    return df


def get_suffix(prec: TargetFrame, succ: TargetFrame) -> Tuple[str, str]:
    return f' "{prec.filter_string}" {uuid4()}', f' "{succ.filter_string}" {uuid4()}'


def get_tf(query: Union[pp.ParseResults, str, TargetFrame],
           edges: Optional[List[str]] = None,
           tf_filter_list: Optional[pd.Series] = None,
           target_filter_list: Optional[pd.Series] = None) -> TargetFrame:
    """
    Query TF DataFrame according to query
    :param query:
    :param edges:
    :param tf_filter_list:
    :param target_filter_list:
    :return:
    """
    if isinstance(query, pp.ParseResults):
        it = iter(query)
        stack = deque()

        try:
            while True:
                curr = next(it)
                if curr in ('and', 'or'):
                    prec, succ = get_tf(stack.pop(), edges, tf_filter_list, target_filter_list), \
                                 get_tf(next(it), edges, tf_filter_list, target_filter_list)

                    filter_string = prec.filter_string
                    if curr == 'and':
                        filter_string += ' and '

                        if prec.include and succ.include:
                            df = prec.merge(succ, how='inner', left_index=True, right_index=True,
                                            suffixes=get_suffix(prec, succ))
                        elif not prec.include and succ.include:
                            df = succ.loc[~succ.index.isin(prec.index), :]
                        elif prec.include and not succ.include:
                            df = prec.loc[~prec.index.isin(succ.index), :]
                        else:  # not prec.include and not succ.include
                            df = prec.merge(succ, how='outer', left_index=True, right_index=True,
                                            suffixes=get_suffix(prec, succ))
                            df.include = False
                    else:
                        filter_string += ' or '

                        # doesn't make much sense using not with or, but oh well
                        if prec.include and succ.include:
                            df = prec.merge(succ, how='outer', left_index=True, right_index=True,
                                            suffixes=get_suffix(prec, succ))
                        elif not prec.include and succ.include:
                            df = succ
                        elif prec.include and not succ.include:
                            df = prec
                        else:
                            df = prec.merge(succ, how='inner', left_index=True, right_index=True,
                                            suffixes=get_suffix(prec, succ))
                            df.include = False
                    filter_string += succ.filter_string

                    try:
                        df = df.groupby(level=[0, 1], axis=1).filter(lambda x: x.notna().any(axis=None))
                    except IndexError:
                        # beware of the shape of indices and columns
                        df = TargetFrame(columns=pd.MultiIndex(levels=[[], [], []], labels=[[], [], []]))

                    df.filter_string = filter_string
                    stack.append(df)

                elif curr == 'not':
                    succ = get_tf(next(it), edges, tf_filter_list, target_filter_list)
                    succ.include = not succ.include
                    succ.filter_string = 'not ' + succ.filter_string
                    stack.append(succ)
                elif is_modifier(curr):
                    prec = get_tf(stack.pop(), edges, tf_filter_list, target_filter_list)
                    mod = get_mod(prec, curr)
                    prec = prec[mod].dropna(how='all')

                    # filter out empty tfs
                    prec = prec.groupby(level=[0, 1], axis=1).filter(lambda x: x.notna().any(axis=None))
                    prec.filter_string += '[' + mod_to_str(curr) + ']'

                    stack.append(prec)
                else:
                    stack.append(curr)
        except StopIteration:
            return get_tf(stack.pop(), edges, tf_filter_list, target_filter_list)
    elif isinstance(query, (TargetFrame, TargetSeries)):
        return query
    else:
        if query.lower() in {'andalltfs', 'oralltfs', 'multitype'}:
            return get_all_tf(query.lower(), edges, tf_filter_list, target_filter_list)

        return get_tf_data(query, edges, tf_filter_list, target_filter_list)


def reorder_data(df: TargetFrame) -> TargetFrame:
    """
    Order by TF with most edges, then analysis with most edges within tf
    :param df:
    :return:
    """
    analysis_order = df.loc[:, (slice(None), slice(None), ['EDGE', 'Log2FC'])].count(
        axis=1, level=1).sum().sort_values(ascending=False)
    tf_order = df.loc[:, (slice(None), slice(None), ['EDGE', 'Log2FC'])].count(
        axis=1, level=0).sum().sort_values(ascending=False)

    return (df.reindex(labels=analysis_order.index, axis=1, level=1)
            .reindex(labels=tf_order.index, axis=1, level=0))


def get_metadata(ids: Sequence) -> TargetFrame:
    analyses = Analysis.objects.filter(pk__in=ids).prefetch_related('analysisdata_set', 'tf')
    df = pd.DataFrame(
        analyses.values_list(
            'id',
            'analysisdata__key__name',
            'analysisdata__value').iterator(),
        columns=['ANALYSIS', 'KEY', 'VALUE'])

    gene_names = pd.DataFrame(
        analyses.values_list('id', 'tf__gene_id', 'tf__name').iterator(),
        columns=['ANALYSIS', 'GENE_ID', 'GENE_NAME']
    ).set_index('ANALYSIS').unstack().reset_index()

    gene_names.columns = ['KEY', 'ANALYSIS', 'VALUE']

    df = pd.concat([df, gene_names], sort=False, ignore_index=True)

    df = df.dropna(how='all', subset=['KEY', 'VALUE'])
    df = df.set_index(['ANALYSIS', 'KEY'])
    df = df.unstack(level=0)
    df.columns = df.columns.droplevel(level=0)

    return df


def get_tf_count(df: TargetFrame) -> TargetSeries:
    counts = df.loc[:, (slice(None), slice(None), ['EDGE', 'Log2FC'])].count(axis=1)
    counts.name = 'TF Count'

    return counts


def expand_ref_ids(df: pd.DataFrame, level: Optional[Union[str, int]] = None) -> pd.DataFrame:
    df = df.copy()

    if level is None:
        analysis_ids = df.columns
    else:
        analysis_ids = df.columns.levels[level]

    full_ids = {a: a.name for a in
                Analysis.objects.filter(
                    pk__in=analysis_ids
                ).prefetch_related('analysisdata_set', 'analysisdata_set__key')}

    if level is None:
        df = df.rename(columns=full_ids)
    elif isinstance(df.columns, pd.MultiIndex):
        df = df.rename(columns=full_ids, level=level)
    else:
        raise ValueError('Please specify level to expand.')

    return df


def parse_query(query: str,
                edges: Optional[List[str]] = None,
                tf_filter_list: Optional[pd.Series] = None,
                target_filter_list: Optional[pd.Series] = None) -> TargetFrame:
    try:
        parse = expr.parseString(query, parseAll=True)

        result = get_tf(parse.get('query'), edges, tf_filter_list, target_filter_list)

        if result.empty or not result.include:
            raise QueryError('empty query')

        return reorder_data(result)
    except pp.ParseException as e:
        raise QueryError("Could not parse query") from e


def get_stats(result: pd.DataFrame) -> Dict[str, Any]:
    return {
        'total': result.loc[:, (slice(None), slice(None), ['EDGE', 'Log2FC'])].groupby(
            level=[0, 1], axis=1).count().sum()
    }


def get_query_result(query: Optional[str] = None,
                     user_lists: Optional[Tuple[pd.DataFrame, Dict]] = None,
                     tf_filter_list: Optional[pd.Series] = None,
                     edges: Optional[List[str]] = None,
                     cache_path: Optional[str] = None,
                     size_limit: Optional[int] = None) -> Tuple[pd.DataFrame, pd.DataFrame, Dict]:
    """
    Get query result from query string or cache

    :param query:
    :param user_lists:
    :param tf_filter_list:
    :param edges:
    :param cache_path:
    :param size_limit:
    :return:
    """
    if query is not None:
        result = parse_query(query, edges, tf_filter_list)
        metadata = get_metadata(result.columns.get_level_values(1))

        stats = get_stats(result)

        if cache_path:
            result.to_pickle(cache_path + '/tabular_output_unfiltered.pickle.gz')

        if user_lists is not None:
            result = result[result.index.isin(user_lists[0].index)].dropna(axis=1, how='all')

        if result.empty:
            raise QueryError("Empty result (user list too restrictive).")

        if cache_path is not None:  # cache here
            result.to_pickle(cache_path + '/tabular_output.pickle.gz')
            metadata.to_pickle(cache_path + '/metadata.pickle.gz')
    elif cache_path is not None:
        result = pd.read_pickle(cache_path + '/tabular_output.pickle.gz')
        metadata = pd.read_pickle(cache_path + '/metadata.pickle.gz')
        stats = get_stats(pd.read_pickle(cache_path + '/tabular_output_unfiltered.pickle.gz'))

        try:
            user_lists = read_cached_result(cache_path + '/target_genes.pickle.gz')
        except FileNotFoundError:
            pass
    else:
        raise ValueError("Need query or cache_path")

    logger.info(f"Unfiltered Dataframe size: {result.size}")

    if size_limit is not None and result.size > size_limit:
        raise QueryError("Result too large.")

    counts = get_tf_count(result)

    result = pd.concat([counts, result], axis=1)

    if user_lists:
        result = user_lists[0].merge(result, left_index=True, right_index=True, how='inner')
        result = result.sort_values(['User List Count', 'User List'])
    else:
        result = pd.concat([
            pd.DataFrame(np.nan, columns=['User List', 'User List Count'], index=result.index),
            result
        ], axis=1)

    result = annotations().drop('id', axis=1).merge(result, how='right', left_index=True, right_index=True)

    result = result.sort_values('TF Count', ascending=False, kind='mergesort')

    logger.info(f"Dataframe size: {result.size}")

    # return statistics here as well
    return result, metadata, stats
