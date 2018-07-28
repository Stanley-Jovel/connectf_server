import gzip
import mimetypes
import pickle
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional, Union
from uuid import UUID

import numpy as np
import pandas as pd
from django.core.serializers.json import DjangoJSONEncoder


class PandasJSONEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, np.number):
            if np.isinf(o) or np.isnan(o):
                return float('inf')
            if isinstance(o, np.integer):
                return int(o)
            if isinstance(o, np.floating):
                return float(o)
        if isinstance(o, pd.Index):
            return o.tolist()

        return super().default(o)


class CytoscapeJSONEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, UUID):
            return str(o)
        return super().default(o)


def cache_result(obj, cache_name: Union[str, Path]):
    encoding = mimetypes.guess_type(cache_name)[1]
    if encoding == 'gzip':
        cache = gzip.open(cache_name, 'wb')
    else:
        cache = open(cache_name, 'wb')

    with closing(cache) as c:
        pickle.dump(obj, c, protocol=pickle.HIGHEST_PROTOCOL)


def read_cached_result(cache_name: Union[str, Path]):
    encoding = mimetypes.guess_type(cache_name)[1]
    if encoding == 'gzip':
        cache = gzip.open(cache_name, 'rb')
    else:
        cache = open(cache_name, 'rb')

    with closing(cache) as c:
        return pickle.load(c)


def convert_float(s) -> Optional[float]:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def metadata_to_dict(df: pd.DataFrame) -> Dict[str, Any]:
    df.reset_index(inplace=True)
    meta_columns = [{"id": column, "name": column, "field": column} for column in df.columns]

    return {'columns': meta_columns,
            'data': df.to_dict(orient='index')}
