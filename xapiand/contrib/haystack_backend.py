from __future__ import absolute_import, unicode_literals

import hashlib

try:
    import cPickle as pickle
except ImportError:
    import pickle

from xapiand import Xapian
from xapiand.core import get_slot, expand_terms, DOCUMENT_CUSTOM_TERM_PREFIX
from xapiand.serialise import LatLongCoord
from xapiand.results import XapianResults

from haystack import connections
from haystack.constants import ID, DEFAULT_ALIAS
from haystack.backends import BaseEngine, BaseSearchBackend, BaseSearchQuery, log_query
from haystack.models import SearchResult
from haystack.utils import get_identifier, get_model_ct

from django.core.exceptions import ImproperlyConfigured

DOCUMENT_CT_FIELD = 'django_ct'
DOCUMENT_AC_FIELD = 'ac'
DOCUMENT_TAGS_FIELD = 'tags'


def consistent_hash(key, num_buckets):
    """
    A Fast, Minimal Memory, Consistent Hash Algorithm (Jump Consistent Hash)

    Hash accepts "a 64-bit key and the number of buckets. It outputs a number
    in the range [0, buckets]." - http://arxiv.org/ftp/arxiv/papers/1406/1406.2294.pdf

    The C++ implementation they provide is as follows:

    int32_t JumpConsistentHash(uint64_t key, int32_t num_buckets) {
        int64_t b = -1, j = 0;
        while (j < num_buckets) {
            b   = j;
            key = key * 2862933555777941757ULL + 1;
            j   = (b + 1) * (double(1LL << 31) / double((key >> 33) + 1));
        }
        return b;
    }

    assert consistent_hash(1, 1) == 0
    assert consistent_hash(256, 1024) == 520
    assert consistent_hash(42, 57) == 43
    assert consistent_hash(0xDEAD10CC, -666) == 0
    assert consistent_hash(0xDEAD10CC, 1) == 0
    assert consistent_hash(0xDEAD10CC, 666) == 361

    """
    if num_buckets == 1:
        return 0
    if not isinstance(key, (int, long)):
        if isinstance(key, unicode):
            key = key.encode('utf-8')
        key = int(hashlib.md5(key).hexdigest(), 16) & 0xffffffffffffffff
    b, j = -1, 0
    if num_buckets < 0:
        num_buckets = 1
    while j < num_buckets:
        b = int(j)
        key = ((key * 2862933555777941757) + 1) & 0xffffffffffffffff
        j = float(b + 1) * (float(1 << 31) / float((key >> 33) + 1))
    return b & 0xffffffff


class XapianSearchResults(XapianResults):
    def get_data(self, result):
        app_label, module_name, pk, model_data = pickle.loads(result['data'].encode('utf-8'))
        return SearchResult(app_label, module_name, pk, result['weight'], **model_data)


class XapianSearchBackend(BaseSearchBackend):
    RESERVED_WORDS = (
        'AND',
        'NOT',
        'OR',
        'LIMIT',
        'OFFSET',
        'TERMS',
        'PARTIAL',
        'ORDER',
        'BY',
    )

    def __init__(self, connection_alias, language=None, **connection_options):
        super(XapianSearchBackend, self).__init__(connection_alias, **connection_options)

        endpoints = connection_options.get('ENDPOINTS', [])
        if not isinstance(endpoints, (tuple, list)):
            endpoints = [endpoints]

        if not endpoints:
            raise ImproperlyConfigured("You must specify 'ENDPOINTS' in your settings for connection '%s'." % connection_alias)
        timeout = connection_options.get('TIMEOUT', None)
        servers = connection_options.get('SERVERS', 'localhost:8890')
        self.xapian = Xapian(servers, using=endpoints, socket_timeout=timeout)
        self.endpoints = endpoints

        self.language = language or connection_options.get('LANGUAGE', 'english')

    def updater(self, index, obj, commit):
        data = index.full_prepare(obj)
        weights = index.get_field_weights()

        document_values = {}
        document_terms = []
        document_texts = []
        for field in self.schema:
            field_name = field['field_name']
            if field_name in data:
                prefix = '%s%s' % (DOCUMENT_CUSTOM_TERM_PREFIX, get_slot(field_name))

                values = data[field_name]
                if not field['multi_valued']:
                    values = [values]

                if not field['stored']:
                    del data[field_name]

                try:
                    weight = int(weights[field_name])
                except KeyError:
                    weight = 1

                field_type = field['type']
                for value in values:
                    if not value:
                        continue

                    if field_type == 'text':
                        if field['mode'] == 'autocomplete':  # mode = content, autocomplete, tagged
                            term_prefix = '%s%s' % (DOCUMENT_CUSTOM_TERM_PREFIX, get_slot(DOCUMENT_AC_FIELD))
                            document_terms.append(dict(term=value, weight=weight, prefix=term_prefix))
                        elif field['mode'] == 'tagged':
                            term_prefix = '%s%s' % (DOCUMENT_CUSTOM_TERM_PREFIX, get_slot(DOCUMENT_TAGS_FIELD))
                            document_terms.append(dict(term=value, weight=weight, prefix=term_prefix))
                        else:
                            document_texts.append(dict(text=value, weight=weight, prefix=prefix))

                    elif field_type in ('ngram', 'edge_ngram'):
                        NGRAM_MIN_LENGTH = 1
                        NGRAM_MAX_LENGTH = 15
                        terms = _ngram_terms({value: weight}, min_length=NGRAM_MIN_LENGTH, max_length=NGRAM_MAX_LENGTH, split=field_type == 'edge_ngram')
                        for term, weight in terms.items():
                            document_terms.append(dict(term=term, weight=weight, prefix=prefix))

                    elif field_type == 'geo_point':
                        lat, _, lng = value.partition(',')
                        value = LatLongCoord(float(lat), float(lng))
                        data[field_name] = value
                        document_values[field_name] = value

                    elif field_type == 'boolean':
                        document_terms.append(dict(term=value, weight=weight, prefix=prefix))

                    elif field_type in ('date', 'integer', 'long', 'float'):
                        document_values[field_name] = value
                        document_terms.append(dict(term=value, weight=weight, prefix=prefix))

                    if field_name == self.content_field_name:
                        pass

        document_data = pickle.dumps((obj._meta.app_label, obj._meta.module_name, obj.pk, data))

        term_prefix = '%s%s' % (DOCUMENT_CUSTOM_TERM_PREFIX, get_slot(DOCUMENT_CT_FIELD))
        document_terms.append(dict(term=get_model_ct(obj), weight=0, prefix=term_prefix))

        document_id = get_identifier(obj)
        endpoint = self.endpoints[consistent_hash(document_id, len(self.endpoints))]
        self.xapian.index(
            id=document_id,
            data=document_data,
            terms=document_terms,
            values=document_values,
            texts=document_texts,
            endpoints=[endpoint],
            positions=True,
        )

    def update(self, index, iterable, commit=False, mod=False):
        for obj in iterable:
            self.updater(index, obj, commit=commit)

    def remove(self, obj, commit=False):
        pass

    def clear(self, models=[], commit=True):
        pass

    @log_query
    def search(self, query_string, start_offset, end_offset=None,
               ranges=None, terms=None, partials=None, models=None,
               **kwargs):
        """
        Returns:
            A dictionary with the following keys:
                `results` -- An iterator of `SearchResult`
                `hits` -- The total available results
                `facets` - A dictionary of facets with the following keys:
                    `fields` -- A list of field facets
                    `dates` -- A list of date facets
                    `queries` -- A list of query facets
            If faceting was not used, the `facets` key will not be present

        """
        offset = start_offset
        limit = end_offset - start_offset
        if limit <= 0:
            return {
                'results': [],
                'hits': 0,
            }

        facets = {
            'fields': {},
            'dates': {},
            'queries': {},
        }

        if models:
            if not terms:
                terms = set()
            terms.update('%s:%s.%s' % (DOCUMENT_CT_FIELD, model._meta.app_label, model._meta.module_name) for model in models)

        query_string = query_string.replace('### AND ###', '').replace('###', '').replace('()', '')
        results = self.xapian.search(
            query_string,
            offset=offset,
            limit=limit,
            results_class=XapianSearchResults,
            ranges=ranges,
            terms=terms,
            partials=partials,
        )
        for facet in results.facets:
            facets['fields'][facet['name']] = (facet['term'], facet['termfreq'])

        return {
            'results': results,
            'facet': facets,
            'hits': results.estimated,
            'size': results.size,
        }

    def build_search_kwargs(self, query_string, sort_by=None, start_offset=0, end_offset=None,
                            fields='', highlight=False, facets=None,
                            date_facets=None, query_facets=None,
                            narrow_queries=None, spelling_query=None,
                            within=None, dwithin=None, distance_point=None,
                            models=None, limit_to_registered_models=None,
                            result_class=None):
        pass

    def build_schema(self, fields):
        """
        Build the schema from fields.

        Required arguments:
            ``fields`` -- A list of fields in the index

        Returns a list of fields in dictionary format ready for inclusion in
        an indexed meta-data.
        """
        content_field_name = ''
        schema_fields = [
            {'field_name': ID, 'type': 'id', 'multi_valued': False, 'column': 0, 'stored': True, 'mode': None},
        ]
        column = len(schema_fields)

        for field_name, field_class in sorted(fields.items(), key=lambda n: n[0]):
            if field_class.document is True:
                content_field_name = field_class.index_fieldname

            if field_class.indexed is True:
                field_data = {
                    'field_name': field_class.index_fieldname,
                    'type': 'text',
                    'multi_valued': False,
                    'column': column,
                    'stored': field_class.stored,
                    'mode': field_class.mode,
                }

                if field_class.field_type in ['date', 'datetime']:
                    field_data['type'] = 'date'
                elif field_class.field_type == 'integer':
                    field_data['type'] = 'long'
                elif field_class.field_type == 'float':
                    field_data['type'] = 'float'
                elif field_class.field_type == 'boolean':
                    field_data['type'] = 'boolean'
                elif field_class.field_type == 'ngram':
                    field_data['type'] = 'ngram'
                elif field_class.field_type == 'edge_ngram':
                    field_data['type'] = 'edge_ngram'
                elif field_class.field_type == 'location':
                    field_data['type'] = 'geo_point'

                if field_class.is_multivalued:
                    field_data['multi_valued'] = True

                schema_fields.append(field_data)
                column += 1

        return (content_field_name, schema_fields)

    @property
    def schema(self):
        if not hasattr(self, '_schema'):
            fields = connections[self.connection_alias].get_unified_index().all_searchfields()
            self._content_field_name, self._schema = self.build_schema(fields)
        return self._schema

    @property
    def content_field_name(self):
        if not hasattr(self, '_content_field_name'):
            fields = connections[self.connection_alias].get_unified_index().all_searchfields()
            self._content_field_name, self._schema = self.build_schema(fields)
        return self._content_field_name


class XapianSearchQuery(BaseSearchQuery):
    def __init__(self, using=DEFAULT_ALIAS):
        super(XapianSearchQuery, self).__init__(using=using)
        self.partials = []
        self.terms = set()
        self.ranges = set()

    def build_params(self, spelling_query=None):
        kwargs = super(XapianSearchQuery, self).build_params(spelling_query=spelling_query)
        if self.terms:
            kwargs['terms'] = self.terms
        if self.partials:
            kwargs['partials'] = self.partials
        if self.ranges:
            kwargs['ranges'] = self.ranges
        return kwargs

    def build_query_fragment(self, field, filter_type, value):
        if filter_type == 'contains':
            value = '%s:%s' % (field, value)

        elif filter_type == 'like':
            self.partials.append(' '.join('%s:%s' % (field, v) for v in value.split()))
            value = '###'

        elif filter_type == 'exact':
            if field == DOCUMENT_AC_FIELD:
                self.partials.append(' '.join('%s:%s' % (field, v) for v in value.split()))
                value = '###'
            elif field == DOCUMENT_TAGS_FIELD:
                self.terms.add(expand_terms(value, field))
                value = '###'
            else:
                value = '%s:"%s"' % (field, value)

        elif filter_type == 'gte':
            self.ranges.add((field, value, None))
            value = '(%s:%s..)' % (field, value)

        elif filter_type == 'gt':
            self.ranges.add((field, None, value))
            value = 'NOT %s' % '(%s:..%s)' % (field, value)

        elif filter_type == 'lte':
            self.ranges.add((field, None, value))
            value = '(%s:..%s)' % (field, value)

        elif filter_type == 'lt':
            self.ranges.add((field, value, None))
            value = 'NOT %s' % '(%s:%s..)' % (field, value)

        elif filter_type == 'startswith':
            value = '%s:%s*' % (field, value)

        elif filter_type == 'in':
            value = '(%s)' % ' OR '.join('%s:%s' % (field, v) for v in value)

        return value


class XapianEngine(BaseEngine):
    backend = XapianSearchBackend
    query = XapianSearchQuery


def _ngram_terms(terms, min_length=1, min_length_percentage=0, max_length=20, split=True):
    """
        :param terms: dictionary of (term, initial weight)
        :param min_length: Minimum ngram length
        :param max_length: Maximum ngram length
        :param min_length_percentage: Minimum length in percentage of the original term
    """
    split_terms = {}
    if not isinstance(terms, dict):
        terms = {terms: 1}

    for term, weight in terms.items():
        if term is not None:
            if split:
                _terms = term.split()
            else:
                _terms = [term]
            for _term in _terms:
                split_terms[_term] = max(split_terms.get(_term, 0), weight)

    # Find all the substrings of the term (all digit terms treated differently):
    final_terms = {}
    for term, weight in split_terms.items():
        term_len = len(term)
        for i in range(term_len):
            for j in range(i + 1, term_len + 1):
                l = j - i
                if l <= max_length and l >= min_length and l >= int(term_len * min_length_percentage):
                    _term = term[i:j]
                    _weight = int(float(weight * l) / term_len)
                    final_terms[_term] = max(final_terms.get(_term, 0), _weight)

    return final_terms
