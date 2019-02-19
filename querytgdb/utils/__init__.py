import base64
import gzip
import logging
import math
import mimetypes
import pickle
import pkgutil
import re
import shutil
import sys
import warnings
from contextlib import closing
from functools import wraps
from operator import methodcaller
from pathlib import Path
from threading import Lock, Thread
from typing import Any, BinaryIO, Callable, Dict, IO, List, Optional, Set, Sized, Tuple, TypeVar, Union
from uuid import UUID

import numpy as np
import pandas as pd
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError
from django.db.models import QuerySet
from django.http import FileResponse
from lxml import etree

from querytgdb.models import Annotation
from ..models import Analysis

logger = logging.getLogger(__name__)

T = TypeVar('T')


class PandasJSONEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, np.number):
            if np.isinf(o):
                return math.inf
            if np.isnan(o):
                return math.nan
            return o.item()
        if isinstance(o, (pd.Index, pd.Series, np.ndarray)):
            return o.tolist()

        return super().default(o)


class NetworkJSONEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, UUID):
            return str(o)
        return super().default(o)


class GzipFileResponse(FileResponse):
    """
    Handles gzip files correctly

    Does not compress files
    """

    def set_headers(self, filelike):
        if isinstance(filelike, gzip.GzipFile) and hasattr(filelike, 'name'):
            filelike.name = None

        return super().set_headers(filelike)


def open_file(path, mode: str) -> IO:
    encoding = mimetypes.guess_type(path)[1]
    if encoding == 'gzip':
        cache = gzip.open(path, mode)
    else:
        cache = open(path, mode)

    return cache


def cache_result(obj: Any, cache_name: Union[str, Path], mode: str = 'wb') -> Union[str, Path]:
    """
    Caches object as gzipped picked
    :param obj:
    :param cache_name:
    :param mode:
    :return:
    """
    cache = open_file(cache_name, mode)

    with closing(cache) as c:
        try:
            shutil.copyfileobj(obj, c)
            obj.seek(0)
        except AttributeError:
            pickle.dump(obj, c, protocol=pickle.HIGHEST_PROTOCOL)

    return cache_name


def read_cached_result(cache_name: Union[str, List[str]], mode: str = 'rb') -> Union[T, IO]:
    """
    Read cached file

    Returns pickled object if possible.
    :param cache_name:
    :param mode:
    :return:
    """
    try:
        cache = open_file(cache_name, mode)

        try:
            with closing(cache) as c:
                return pickle.load(c)
        except pickle.UnpicklingError:
            return open_file(cache_name, mode)
    except TypeError:
        for p in cache_name:
            try:
                return read_cached_result(p, mode)
            except FileNotFoundError:
                pass

        raise FileNotFoundError(f"file not in paths: {', '.join(cache_name)}")


def cache_view(func: Callable[..., T], cache_path: str, dummy_cache: bool = False) -> T:
    """
    read results from cache if possible
    :param func:
    :param cache_path:
    :param dummy_cache:
    :return:
    """
    try:
        result = read_cached_result(cache_path)
    except FileNotFoundError:
        result = func()
        if not dummy_cache:
            cache_result(result, cache_path)

    return result


def convert_float(s) -> Optional[float]:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def metadata_to_dict(df: pd.DataFrame) -> Dict[str, Any]:
    df = df.rename(columns=lambda x: f"ID: {x}").reset_index()
    meta_columns = [{"id": column, "name": column, "field": column} for column in df.columns]

    df = df.where(df.notna(), None)

    return {'columns': meta_columns,
            'data': df.to_dict(orient='index')}


def get_exp_type(metadata: pd.Series) -> str:
    if metadata['EXPERIMENT_TYPE'] == 'EXPRESSION':
        return metadata['EXPRESSION_TYPE']
    elif metadata['EXPERIMENT_TYPE'] == 'BINDING':
        return metadata['BINDING_TYPE']
    else:
        raise ValueError('Invalid EXPERIMENT_TYPE')


def column_string(n: int) -> str:
    s = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        s = chr(65 + remainder) + s
    return s


def svg_font_adder(buff: BinaryIO) -> BinaryIO:
    """
    Adds font file as a base64 encoded string to an SVG

    The very definition of overkill
    :param buff:
    :return:
    """
    tree = etree.parse(buff)
    style = tree.find('./{http://www.w3.org/2000/svg}defs/{http://www.w3.org/2000/svg}style')

    font_str = base64.standard_b64encode(pkgutil.get_data('matplotlib', 'mpl-data/fonts/ttf/DejaVuSans.ttf')).decode()

    style.text += '@font-face {{font-family: "DejaVu Sans"; src: local("DejaVu Sans"), local("DejaVuSans"), ' \
                  'url({}) format("truetype");}}'.format('data:font/ttf;base64,' + font_str)

    buff.truncate(0)
    tree.write(buff)
    buff.seek(0)

    return buff


NAME_REGEX = re.compile(r"^([^\"]+)(?:\s*\"([^\"]+)\")?")


def split_name(name: str) -> Tuple[str, str]:
    m = NAME_REGEX.match(name)

    if m:
        name, criterion = map(methodcaller('strip'), m.groups(''))
    else:
        criterion = ''

    return name, criterion


def clear_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove unneeded columns when doing later calculations
    :param df:
    :return:
    """
    df = df.loc[:, (slice(None), slice(None), ['EDGE', 'Log2FC'])]
    df.columns = df.columns.droplevel(2)

    return df


def data_to_edges(df: pd.DataFrame, analyses: Optional[QuerySet] = None, drop: bool = True) -> pd.DataFrame:
    """
    Convert EDGE and Log2FC to respective edge_type
    :param df:
    :param analyses:
    :param drop:
    :return:
    """
    df = df.loc[:, (slice(None), slice(None), ['EDGE', 'Log2FC'])]

    if analyses is None:
        analyses = Analysis.objects.filter(
            pk__in=df.columns.get_level_values(1)
        ).prefetch_related('analysisdata_set', 'analysisdata_set__key')

    def set_edge_name(s):
        try:
            edge_type = analyses.get(pk=s.name[1]).analysisdata_set.get(key__name='EDGE_TYPE').value
        except (ObjectDoesNotExist, MultipleObjectsReturned):
            edge_type = 'edge'

        if s.name[2] == 'Log2FC':
            c = s.mask(s >= 0, edge_type + ':INDUCED')
            c = c.mask(s < 0, edge_type + ':REPRESSED')

            return c
        else:
            return s.mask(s.notna(), edge_type)

    df = df.apply(set_edge_name)

    if drop:
        df.columns = df.columns.droplevel(2)

    return df


def get_size(func: Callable[..., Sized]) -> Callable[..., Sized]:
    """
    Get size of function out put

    :param func:
    :return:
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)

        logger.info(f"{func.__name__} len: {len(result)} size: {sys.getsizeof(result)}")

        return result

    return wrapper


def read_from_cache(func: Callable[..., T]) -> Callable[..., T]:
    """
    read argument from cache file and pass as parameter

    :param func:
    :return:
    """

    @wraps(func)
    def wrapper(*args: str, **kwargs):
        return func(*map(read_cached_result, args), **kwargs)

    return wrapper


class DatabaseWarning(UserWarning):
    pass


class Annotations:
    def __init__(self):
        self._anno: pd.DataFrame = None

        self.lock = Lock()
        self.task = Thread(target=self.get_annotations)
        self.task.start()

    def get_annotations(self):
        """
        Loads annotations from the database into memory.
        """
        try:
            anno = pd.DataFrame(
                Annotation.objects.values_list(
                    'gene_id', 'fullname', 'gene_family', 'gene_type', 'name', 'id').iterator(),
                columns=['TARGET', 'Full Name', 'Gene Family', 'Type', 'Name', 'id'])
            anno = anno.set_index('TARGET')
        except DatabaseError:
            warnings.warn(DatabaseWarning("No annotation data."))

            anno = pd.DataFrame(columns=['Full Name', 'Gene Family', 'Type', 'Name', 'id'])
            anno.index.name = 'TARGET'

        with self.lock:
            self._anno = anno

    def __call__(self):
        if self._anno is None:
            self.task.join()

        return self._anno

    @property
    def genes(self) -> Set:
        if self._anno is None:
            self()

        return set(self._anno.index)


annotations = Annotations()


def check_annotations(genes):
    if isinstance(genes, set):
        return genes - annotations.genes

    return set(genes) - annotations.genes
