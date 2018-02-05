""" Elatic Search implementation for courseware search index """
import copy
import logging

from django.conf import settings
from django.core.cache import cache
from elasticsearch import Elasticsearch, exceptions
from elasticsearch.helpers import bulk

from search.search_engine_base import SearchEngine
from search.utils import ValueRange, _is_iterable

# log appears to be standard name used for logger
log = logging.getLogger(__name__)  # pylint: disable=invalid-name

# These are characters that may have special meaning within Elasticsearch.
# We _may_ want to use these for their special uses for certain queries,
# but for analysed fields these kinds of characters are removed anyway, so
# we can safely remove them from analysed matches
RESERVED_CHARACTERS = "+-=><!(){}[]^\"~*:\\/&|?"


def _translate_hits(es_response):
    """ Provide resultset in our desired format from elasticsearch results """

    def translate_result(result):
        """ Any conversion from ES result syntax into our search engine syntax """
        translated_result = copy.copy(result)
        data = translated_result.pop("_source")

        translated_result.update({
            "data": data,
            "score": translated_result["_score"]
        })

        return translated_result

    def translate_facet(result):
        """ Any conversion from ES facet syntax into our search engine sytax """
        terms = {term["term"]: term["count"] for term in result["terms"]}
        return {
            "terms": terms,
            "total": result["total"],
            "other": result["other"],
        }

    results = [translate_result(hit) for hit in es_response["hits"]["hits"]]
    response = {
        "took": es_response["took"],
        "total": es_response["hits"]["total"],
        "max_score": es_response["hits"]["max_score"],
        "results": results,
    }

    if "facets" in es_response:
        response["facets"] = {facet: translate_facet(es_response["facets"][facet]) for facet in es_response["facets"]}

    return response


def _get_filter_field(field_name, field_value):
    """ Return field to apply into filter, if an array then use a range, otherwise look for a term match """
    filter_field = None
    if isinstance(field_value, ValueRange):
        range_values = {}
        if field_value.lower:
            range_values.update({"gte": field_value.lower_string})
        if field_value.upper:
            range_values.update({"lte": field_value.upper_string})
        filter_field = {
            "range": {
                field_name: range_values
            }
        }
    elif _is_iterable(field_value):
        filter_field = {
            "terms": {
                field_name: field_value
            }
        }
    else:
        filter_field = {
            "term": {
                field_name: field_value
            }
        }
    return filter_field


def _process_field_queries(field_dictionary):
    """
    We have a field_dictionary - we want to match the values for an elasticsearch "match" query
    This is only potentially useful when trying to tune certain search operations
    """
    def field_item(field):
        """ format field match as "match" item for elasticsearch query """
        return {
            "match": {
                field: field_dictionary[field]
            }
        }

    return [field_item(field) for field in field_dictionary]


def _process_field_filters(field_dictionary):
    """
    We have a field_dictionary - we match the values using a "term" filter in elasticsearch
    """
    return [_get_filter_field(field, field_value) for field, field_value in field_dictionary.items()]


def _process_filters(filter_dictionary):
    """
    We have a filter_dictionary - this means that if the field is included
    and matches, then we can include, OR if the field is undefined, then we
    assume it is safe to include
    """
    def filter_item(field):
        """ format elasticsearch filter to pass if value matches OR field is not included """
        if filter_dictionary[field] is not None:
            return {
                "or": [
                    _get_filter_field(field, filter_dictionary[field]),
                    {
                        "missing": {
                            "field": field
                        }
                    }
                ]
            }
        else:
            return {
                "missing": {
                    "field": field
                }
            }

    return [filter_item(field) for field in filter_dictionary]


def _process_exclude_dictionary(exclude_dictionary):
    """
    Based on values in the exclude_dictionary generate a list of term queries that
    will filter out unwanted results.
    """
    # not_properties will hold the generated term queries.
    not_properties = []
    for exclude_property in exclude_dictionary:
        exclude_values = exclude_dictionary[exclude_property]
        if not isinstance(exclude_values, list):
            exclude_values = [exclude_values]
        not_properties.extend([{"term": {exclude_property: exclude_value}} for exclude_value in exclude_values])

    # Returning a query segment with an empty list freaks out ElasticSearch,
    #   so just return an empty segment.
    if not not_properties:
        return {}

    return {
        "not": {
            "filter": {
                "or": not_properties
            }
        }
    }


def _process_facet_terms(facet_terms):
    """ We have a list of terms with which we return facets """
    elastic_facets = {}
    for facet in facet_terms:
        facet_term = {"field": facet}
        if facet_terms[facet]:
            for facet_option in facet_terms[facet]:
                facet_term[facet_option] = facet_terms[facet][facet_option]

        elastic_facets[facet] = {
            "terms": facet_term
        }

    return elastic_facets


class ElasticSearchEngine(SearchEngine):

    """ ElasticSearch implementation of SearchEngine abstraction """

    @staticmethod
    def get_cache_item_name(index_name, doc_type):
        """ name-formatter for cache_item_name """
        return "elastic_search_mappings_{}_{}".format(
            index_name,
            doc_type
        )

    @classmethod
    def get_mappings(cls, index_name, doc_type):
        """ fetch mapped-items structure from cache """
        return cache.get(cls.get_cache_item_name(index_name, doc_type), {})

    @classmethod
    def set_mappings(cls, index_name, doc_type, mappings):
        """ set new mapped-items structure into cache """
        cache.set(cls.get_cache_item_name(index_name, doc_type), mappings)

    @classmethod
    def log_indexing_error(cls, indexing_errors):
        """ Logs indexing errors and raises a general ElasticSearch Exception"""
        indexing_errors_log = []
        for indexing_error in indexing_errors:
            indexing_errors_log.append(indexing_error.message)
        raise exceptions.ElasticsearchException(', '.join(indexing_errors_log))

    def _get_mappings(self, doc_type):
        """
        Interfaces with the elasticsearch mappings for the index
        prevents multiple loading of the same mappings from ES when called more than once

        Mappings format in elasticsearch is as follows:
        {
           "doc_type": {
              "properties": {
                 "nested_property": {
                    "properties": {
                       "an_analysed_property": {
                          "type": "string"
                       },
                       "another_analysed_property": {
                          "type": "string"
                       }
                    }
                 },
                 "a_not_analysed_property": {
                    "type": "string",
                    "index": "not_analyzed"
                 },
                 "a_date_property": {
                    "type": "date"
                 }
              }
           }
        }

        We cache the properties of each doc_type, if they are not available, we'll load them again from Elasticsearch
        """
        doc_mappings = ElasticSearchEngine.get_mappings(self.index_name, doc_type)
        if not doc_mappings:
            try:
                doc_mappings = self._es.indices.get_mapping(
                    index=self.index_name,
                    doc_type=doc_type,
                )[doc_type]
                ElasticSearchEngine.set_mappings(
                    self.index_name,
                    doc_type,
                    doc_mappings
                )
            except exceptions.NotFoundError:
                # In this case there are no mappings for this doc_type on the elasticsearch server
                # This is a normal case when a new doc_type is being created, and it is expected that
                # we'll hit it for new doc_type s
                return {}

        return doc_mappings

    def _clear_mapping(self, doc_type):
        """ Remove the cached mappings, so that they get loaded from ES next time they are requested """
        ElasticSearchEngine.set_mappings(self.index_name, doc_type, {})

    def __init__(self, index=None):
        super(ElasticSearchEngine, self).__init__(index)
        es_config = getattr(settings, "ELASTIC_SEARCH_CONFIG", [{}])
        self._es = getattr(settings, "ELASTIC_SEARCH_IMPL", Elasticsearch)(es_config)
        if not self._es.indices.exists(index=self.index_name):
            self._es.indices.create(index=self.index_name)

    def _check_mappings(self, doc_type, body):
        """
        We desire to index content so that anything we want to be textually searchable(and therefore needing to be
        analysed), but the other fields are designed to be filters, and only require an exact match. So, we want to
        set up the mappings for these fields as "not_analyzed" - this will allow our filters to work faster because
        they only have to work off exact matches
        """

        # Make fields other than content be indexed as unanalyzed terms - content
        # contains fields that are to be analyzed
        exclude_fields = ["content"]
        field_properties = getattr(settings, "ELASTIC_FIELD_MAPPINGS", {})

        def field_property(field_name, field_value):
            """
            Prepares field as property syntax for providing correct mapping desired for field

            Mappings format in elasticsearch is as follows:
            {
               "doc_type": {
                  "properties": {
                     "nested_property": {
                        "properties": {
                           "an_analysed_property": {
                              "type": "string"
                           },
                           "another_analysed_property": {
                              "type": "string"
                           }
                        }
                     },
                     "a_not_analysed_property": {
                        "type": "string",
                        "index": "not_analyzed"
                     },
                     "a_date_property": {
                        "type": "date"
                     }
                  }
               }
            }

            We can only add new ones, but the format is the same
            """
            prop_val = None
            if field_name in field_properties:
                prop_val = field_properties[field_name]
            elif isinstance(field_value, dict):
                props = {fn: field_property(fn, field_value[fn]) for fn in field_value}
                prop_val = {"properties": props}
            else:
                prop_val = {
                    "type": "string",
                    "index": "not_analyzed",
                }

            return prop_val

        new_properties = {
            field: field_property(field, value)
            for field, value in body.items()
            if (field not in exclude_fields) and (field not in self._get_mappings(doc_type).get('properties', {}))
        }

        if new_properties:
            self._es.indices.put_mapping(
                index=self.index_name,
                doc_type=doc_type,
                body={
                    doc_type: {
                        "properties": new_properties,
                    }
                }
            )
            self._clear_mapping(doc_type)

    def index(self, doc_type, sources, **kwargs):
        """
        Implements call to add documents to the ES index
        Note the call to _check_mappings which will setup fields with the desired mappings
        """

        try:
            actions = []
            for source in sources:
                self._check_mappings(doc_type, source)
                id_ = source['id'] if 'id' in source else None
                log.debug("indexing %s object with id %s", doc_type, id_)
                action = {
                    "_index": self.index_name,
                    "_type": doc_type,
                    "_id": id_,
                    "_source": source
                }
                actions.append(action)
            # bulk() returns a tuple with summary information
            # number of successfully executed actions and number of errors if stats_only is set to True.
            _, indexing_errors = bulk(
                self._es,
                actions,
                **kwargs
            )
            if indexing_errors:
                ElasticSearchEngine.log_indexing_error(indexing_errors)
        # Broad exception handler to protect around bulk call
        except Exception as ex:
            # log information and re-raise
            log.exception("error while indexing - %s", ex.message)
            raise

    def remove(self, doc_type, doc_ids, **kwargs):
        """ Implements call to remove the documents from the index """

        try:
            # ignore is flagged as an unexpected-keyword-arg; ES python client documents that it can be used
            # pylint: disable=unexpected-keyword-arg
            actions = []
            for doc_id in doc_ids:
                log.debug("remove index for %s object with id %s", doc_type, doc_id)
                action = {
                    '_op_type': 'delete',
                    "_index": self.index_name,
                    "_type": doc_type,
                    "_id": doc_id
                }
                actions.append(action)
            # bulk() returns a tuple with summary information
            # number of successfully executed actions and number of errors if stats_only is set to True.
            _, indexing_errors = bulk(
                self._es,
                actions,
                # let notfound not cause error
                ignore=[404],
                **kwargs
            )
            if indexing_errors:
                ElasticSearchEngine.log_indexing_error(indexing_errors)
        # Broad exception handler to protect around bulk call
        except Exception as ex:
            # log information and re-raise
            log.exception("error while deleting document from index - %s", ex.message)
            raise ex

    # A few disabled pylint violations here:
    # This procedure takes each of the possible input parameters and builds the query with each argument
    # I tried doing this in separate steps, but IMO it makes it more difficult to follow instead of less
    # So, reasoning:
    #
    #   too-many-arguments: We have all these different parameters to which we
    #       wish to pay attention, it makes more sense to have them listed here
    #       instead of burying them within kwargs
    #
    #   too-many-locals: I think this counts all the arguments as well, but
    #       there are some local variables used herein that are there for transient
    #       purposes and actually promote the ease of understanding
    #
    #   too-many-branches: There's a lot of logic on the 'if I have this
    #       optional argument then...'. Reasoning goes back to its easier to read
    #       the (somewhat linear) flow rather than to jump up to other locations in code
    def search(self,
               query_string=None,
               field_dictionary=None,
               filter_dictionary=None,
               exclude_dictionary=None,
               facet_terms=None,
               exclude_ids=None,
               use_field_match=False,
               **kwargs):  # pylint: disable=too-many-arguments, too-many-locals, too-many-branches
        """
        Implements call to search the index for the desired content.

        Args:
            query_string (str): the string of values upon which to search within the
            content of the objects within the index

            field_dictionary (dict): dictionary of values which _must_ exist and
            _must_ match in order for the documents to be included in the results

            filter_dictionary (dict): dictionary of values which _must_ match if the
            field exists in order for the documents to be included in the results;
            documents for which the field does not exist may be included in the
            results if they are not otherwise filtered out

            exclude_dictionary(dict): dictionary of values all of which which must
            not match in order for the documents to be included in the results;
            documents which have any of these fields and for which the value matches
            one of the specified values shall be filtered out of the result set

            facet_terms (dict): dictionary of terms to include within search
            facets list - key is the term desired to facet upon, and the value is a
            dictionary of extended information to include. Supported right now is a
            size specification for a cap upon how many facet results to return (can
            be an empty dictionary to use default size for underlying engine):

            e.g.
            {
                "org": {"size": 10},  # only show top 10 organizations
                "modes": {}
            }

            use_field_match (bool): flag to indicate whether to use elastic
            filtering or elastic matching for field matches - this is nothing but a
            potential performance tune for certain queries

            (deprecated) exclude_ids (list): list of id values to exclude from the results -
            useful for finding maches that aren't "one of these"

        Returns:
            dict object with results in the desired format
            {
                "took": 3,
                "total": 4,
                "max_score": 2.0123,
                "results": [
                    {
                        "score": 2.0123,
                        "data": {
                            ...
                        }
                    },
                    {
                        "score": 0.0983,
                        "data": {
                            ...
                        }
                    }
                ],
                "facets": {
                    "org": {
                        "total": total_count,
                        "other": 1,
                        "terms": {
                            "MITx": 25,
                            "HarvardX": 18
                        }
                    },
                    "modes": {
                        "total": modes_count,
                        "other": 15,
                        "terms": {
                            "honor": 58,
                            "verified": 44,
                        }
                    }
                }
            }

        Raises:
            ElasticsearchException when there is a problem with the response from elasticsearch

        Example usage:
            .search(
                "find the words within this string",
                {
                    "must_have_field": "mast_have_value for must_have_field"
                },
                {

                }
            )
        """

        log.debug("searching index with %s", query_string)

        elastic_queries = []
        elastic_filters = []

        # We have a query string, search all fields for matching text within the "content" node
        if query_string:
            elastic_queries.append({
                "query_string": {
                    "fields": ["display_name^4", "number", "short_description^2", "overview^0.2"],
                    #"fields": ["content.*"],
                    "query": query_string.encode('utf-8').translate(None, RESERVED_CHARACTERS)
                }
            })

        if field_dictionary:
            if use_field_match:
                elastic_queries.extend(_process_field_queries(field_dictionary))
            else:
                elastic_filters.extend(_process_field_filters(field_dictionary))

        if filter_dictionary:
            elastic_filters.extend(_process_filters(filter_dictionary))

        # Support deprecated argument of exclude_ids
        if exclude_ids:
            if not exclude_dictionary:
                exclude_dictionary = {}
            if "_id" not in exclude_dictionary:
                exclude_dictionary["_id"] = []
            exclude_dictionary["_id"].extend(exclude_ids)

        if exclude_dictionary:
            elastic_filters.append(_process_exclude_dictionary(exclude_dictionary))

        query_segment = {
            "match_all": {}
        }
        if elastic_queries:
            query_segment = {
                "bool": {
                    "must": elastic_queries
                }
            }

        query = query_segment
        if elastic_filters:
            filter_segment = {
                "bool": {
                    "must": elastic_filters
                }
            }
            query = {
                "filtered": {
                    "query": query_segment,
                    "filter": filter_segment,
                }
            }

        #body = {"query": query}
        body = {"sort":["_score"], "query": query}
        if facet_terms:
            facet_query = _process_facet_terms(facet_terms)
            if facet_query:
                body["facets"] = facet_query

        try:
            es_response = self._es.search(
                index=self.index_name,
                body=body,
                **kwargs
            )
        except exceptions.ElasticsearchException as ex:
            # log information and re-raise
            log.exception("error while searching index - %s", ex.message)
            raise

        return _translate_hits(es_response)

    # search method override : add parameter pagepos
    def search(self,
               query_string=None,
               field_dictionary=None,
               filter_dictionary=None,
               exclude_dictionary=None,
               facet_terms=None,
               exclude_ids=None,
               pagepos=None,
               classfysub=None,
               middle_classfysub=None,
               use_field_match=False,
               **kwargs):  # pylint: disable=too-many-arguments, too-many-locals, too-many-branches

        log.debug("searching index with %s", query_string)

        elastic_queries = []
        elastic_filters = []

        # We have a query string, search all fields for matching text within the "content" node
        if query_string:
            elastic_queries.append({
                "query_string": {
                    "fields": ["display_name^4", "number", "short_description^2", "overview^0.2"],
                    #"fields": ["content.*"],
                    "query": query_string.encode('utf-8').translate(None, RESERVED_CHARACTERS)
                }
            })

        if field_dictionary:
            if use_field_match:
                elastic_queries.extend(_process_field_queries(field_dictionary))
            else:
                elastic_filters.extend(_process_field_filters(field_dictionary))

        if filter_dictionary:
            elastic_filters.extend(_process_filters(filter_dictionary))

        # Support deprecated argument of exclude_ids
        if exclude_ids:
            if not exclude_dictionary:
                exclude_dictionary = {}
            if "_id" not in exclude_dictionary:
                exclude_dictionary["_id"] = []
            exclude_dictionary["_id"].extend(exclude_ids)

        if exclude_dictionary:
            elastic_filters.append(_process_exclude_dictionary(exclude_dictionary))

        query_segment = {
            "match_all": {}
        }

        # "term": {"catalog_visibility": "none about"}
        # [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}]
        query_sub_segment = {}
        if pagepos == 'l':
            query_sub_segment = {
                    "term": {"catalog_visibility": "none about"}
            }
        elif pagepos == 'd':
            query_sub_segment = {
                    "term": {"catalog_visibility": "none"}
            }

        if elastic_queries:
            query_segment = {
                "bool": {
                    "must": elastic_queries
                }
            }

        query = query_segment

        if elastic_filters:
            if classfysub != None and classfysub != '' and middle_classfysub != None and middle_classfysub != '':
                if len(query_sub_segment)>0:
                    if len(classfysub)>0 and len(middle_classfysub) > 0:
                        classfysub.replace(',',' ')
                        middle_classfysub.replace(',',' ')
                        filter_segment = {
                            "bool": {
                                "should": [{"term": {"classfy": classfysub}}, {"term": {"classfysub": classfysub} }, {"term": {"middle_classfy": middle_classfysub}}, {"term": {"middle_classfysub": middle_classfysub}}],
                                "must_not": [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}],
                                "must": elastic_filters
                            }
                        }
                    else:
                        filter_segment = {
                            "bool": {
                                "must_not": [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}],
                                "must": elastic_filters
                            }
                        }
                else:
                    if len(classfysub)>0 and len(middle_classfysub) > 0:
                        classfysub.replace(',',' ')
                        middle_classfysub.replace(',',' ')
                        filter_segment = {
                            "bool": {
                                "should": [{"term": {"classfy": classfysub}}, {"term": {"classfysub": classfysub}}, {"term": {"middle_classfy": middle_classfysub}},{"term": {"middle_classfysub": middle_classfysub}}],
                                "must": elastic_filters
                            }
                        }
                    else:
                        filter_segment = {
                            "bool": {
                                "must": elastic_filters
                            }
                        }
            elif (classfysub == '' or classfysub == None) and (middle_classfysub != None and middle_classfysub != ''):
                if len(query_sub_segment)>0:
                    if len(middle_classfysub)>0:
                        middle_classfysub.replace(',',' ')
                        filter_segment = {
                            "bool": {
                                "should": [{"term": {"middle_classfy": middle_classfysub} },{ "term": {"middle_classfysub": middle_classfysub}}],
                                "must_not": [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}],
                                "must": elastic_filters
                            }
                        }
                    else:
                        filter_segment = {
                            "bool": {
                                "must_not": [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}],
                                "must": elastic_filters
                            }
                        }
                else:
                    if len(middle_classfysub)>0:
                        middle_classfysub.replace(',',' ')
                        filter_segment = {
                            "bool": {
                                "should": [{"term": {"middle_classfy": middle_classfysub} },{ "term": {"middle_classfysub": middle_classfysub}}],
                                "must": elastic_filters
                            }
                        }
                    else:
                        filter_segment = {
                            "bool": {
                                "must": elastic_filters
                            }
                        }
            elif (middle_classfysub == '' or middle_classfysub == None) and (classfysub != None and classfysub != ''):
                if len(query_sub_segment)>0:
                    if len(classfysub)>0:
                        classfysub.replace(',',' ')
                        filter_segment = {
                            "bool": {
                                "should": [{"term": {"classfy": classfysub} },{ "term": {"classfysub": classfysub}}],
                                "must_not": [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}],
                                "must": elastic_filters
                            }
                        }
                    else:
                        filter_segment = {
                            "bool": {
                                "must_not": [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}],
                                "must": elastic_filters
                            }
                        }
                else:
                    if len(classfysub)>0:
                        classfysub.replace(',',' ')
                        filter_segment = {
                            "bool": {
                                "should": [{"term": {"classfy": classfysub} },{ "term": {"classfysub": classfysub}}],
                                "must": elastic_filters
                            }
                        }
                    else:
                        filter_segment = {
                            "bool": {
                                "must": elastic_filters
                            }
                        }
            else:
                if len(query_sub_segment)>0:
                    filter_segment = {
                        "bool": {
                            "must_not": [{"term": {"catalog_visibility": "none"}}, {"term": {"catalog_visibility": "about"}}],
                            "must": elastic_filters
                        }
                    }
                else:
                    filter_segment = {
                        "bool": {
                            "must": elastic_filters
                        }
                    }


        if pagepos != '' and pagepos != None:
            if elastic_filters:
                query = {
                    "filtered": {
                        "query": query_segment,
                        "filter": filter_segment,
                    }
                }
        else:
            if elastic_filters:
                query = {
                    "filtered": {
                        "query": query_segment,
                        "filter": filter_segment,
                    }
                }

        #body = {"query": query}
        body = {"sort":["_score"], "query": query}

        if facet_terms:
            facet_query = _process_facet_terms(facet_terms)
            if facet_query:
                body["facets"] = facet_query

        try:
            es_response = self._es.search(
                index=self.index_name,
                body=body,
                **kwargs
            )
        except exceptions.ElasticsearchException as ex:
            # log information and re-raise
            log.exception("error while searching index - %s", ex.message)
            raise

        return _translate_hits(es_response)
