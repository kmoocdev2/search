#-*- coding: utf-8 -*-
""" search business logic implementations """
from datetime import datetime

from django.conf import settings

from .filter_generator import SearchFilterGenerator
from .search_engine_base import SearchEngine
from .result_processor import SearchResultProcessor
from .utils import DateRange

# Default filters that we support, override using COURSE_DISCOVERY_FILTERS setting if desired
DEFAULT_FILTER_FIELDS = ["org", "language", "modes", "classfy", "middle_classfy", "classfysub", "middle_classfysub", "linguistics", "range", "course_period", "org_kname", "org_ename", "teacher_name"]


def course_discovery_filter_fields():
    """ look up the desired list of course discovery filter fields """
    return getattr(settings, "COURSE_DISCOVERY_FILTERS", DEFAULT_FILTER_FIELDS)


def course_discovery_facets():
    """ Discovery facets to include, by default we specify each filter field with unspecified size attribute """
    facets = ['org', 'language', 'modes', 'classfy', 'middle_classfy', 'classfysub', 'middle_classfysub', 'linguistics', 'range', 'course_period', 'org_kname', 'org_ename', 'teacher_name']
    return getattr(settings, "COURSE_DISCOVERY_FACETS", {field: {'size':'300'} for field in facets})


class NoSearchEngineError(Exception):
    """ NoSearchEngineError exception to be thrown if no search engine is specified """
    pass


class QueryParseError(Exception):
    """QueryParseError will be thrown if the query is malformed.

    If a query has mismatched quotes (e.g. '"some phrase', return a
    more specific exception so the view can provide a more helpful
    error message to the user.

    """
    pass


def perform_search(
        search_term,
        user=None,
        size=10,
        from_=0,
        course_id=None):
    """ Call the search engine with the appropriate parameters """
    # field_, filter_ and exclude_dictionary(s) can be overridden by calling application
    # field_dictionary includes course if course_id provided
    (field_dictionary, filter_dictionary, exclude_dictionary) = SearchFilterGenerator.generate_field_filters(
        user=user,
        course_id=course_id
    )

    searcher = SearchEngine.get_search_engine(getattr(settings, "COURSEWARE_INDEX_NAME", "courseware_index"))
    if not searcher:
        raise NoSearchEngineError("No search engine specified in settings.SEARCH_ENGINE")

    results = searcher.search_string(
        search_term,
        field_dictionary=field_dictionary,
        filter_dictionary=filter_dictionary,
        exclude_dictionary=exclude_dictionary,
        size=size,
        from_=from_,
        doc_type="courseware_content",
    )

    # post-process the result
    for result in results["results"]:
        result["data"] = SearchResultProcessor.process_result(result["data"], search_term, user)

    results["access_denied_count"] = len([r for r in results["results"] if r["data"] is None])
    results["results"] = [r for r in results["results"] if r["data"] is not None]

    return results


#def course_discovery_search(search_term=None, size=20, from_=0, field_dictionary=None):
#    """
#    Course Discovery activities against the search engine index of course details
#    """
#    # We'll ignore the course-enrollemnt informaiton in field and filter
#    # dictionary, and use our own logic upon enrollment dates for these
#    use_search_fields = ["org"]
#    (search_fields, _, exclude_dictionary) = SearchFilterGenerator.generate_field_filters()
#    use_field_dictionary = {}
#    use_field_dictionary.update({field: search_fields[field] for field in search_fields if field in use_search_fields})
#    if field_dictionary:
#        use_field_dictionary.update(field_dictionary)
#    if not getattr(settings, "SEARCH_SKIP_ENROLLMENT_START_DATE_FILTERING", False):
#        use_field_dictionary["enrollment_start"] = DateRange(None, datetime.utcnow())
#
#    searcher = SearchEngine.get_search_engine(getattr(settings, "COURSEWARE_INDEX_NAME", "courseware_index"))
#    if not searcher:
#        raise NoSearchEngineError("No search engine specified in settings.SEARCH_ENGINE")
#
#    results = searcher.search(
#        query_string=search_term,
#        doc_type="course_info",
#        size=size,
#        from_=from_,
#        # only show when enrollment start IS provided and is before now
#        field_dictionary=use_field_dictionary,
#        # show if no enrollment end is provided and has not yet been reached
#        filter_dictionary={"enrollment_end": DateRange(datetime.utcnow(), None)},
#        exclude_dictionary=exclude_dictionary,
#        facet_terms=course_discovery_facets(),
#    )
#
#    return results

def course_discovery_search(search_term=None, size=20, from_=0, field_dictionary=None):
    """
    Course Discovery activities against the search engine index of course details
    """
    # We'll ignore the course-enrollemnt informaiton in field and filter
    # dictionary, and use our own logic upon enrollment dates for these
    #use_search_fields = ["org"]
    use_search_fields = ["org", "language", "modes", 'classfy', 'middle_classfy', 'classfysub', 'middle_classfysub', 'linguistics', 'range', 'course_period', 'start', 'org_kname', 'org_ename', 'teacher_name']
    (search_fields, _, exclude_dictionary) = SearchFilterGenerator.generate_field_filters()
    use_field_dictionary = {}
    use_field_dictionary.update({field: search_fields[field] for field in search_fields if field in use_search_fields})

    # --------------- adding --------------- #
    if 'range' in field_dictionary:
        range_val = field_dictionary['range']
        
        print ("range_val:",range_val)

        del(field_dictionary['range'])

        if range_val == 'i':
            use_field_dictionary['start'] = DateRange(None, datetime.utcnow())
            use_field_dictionary['end'] = DateRange(datetime.utcnow(), None)
        elif range_val == 'a':
            use_field_dictionary['audit_yn'] = 'Y'
            use_field_dictionary['end'] = DateRange(None, datetime.utcnow())
        elif range_val == 'e':
            use_field_dictionary['audit_yn'] = 'N'
            use_field_dictionary['end'] = DateRange(None, datetime.utcnow())
        elif range_val == 't':
            use_field_dictionary['start'] = DateRange(datetime.utcnow(), None)
    # --------------- adding --------------- #
    if 'classfy' in field_dictionary:
        classfy_val = field_dictionary['classfy']
        del (field_dictionary['classfy'])
    else:
        classfy_val = ''

    if 'middle_classfy' in field_dictionary:
        middle_classfy_val = field_dictionary['middle_classfy']
        del (field_dictionary['middle_classfy'])
    else:
        middle_classfy_val = ''
    # --------------- adding --------------- #

    if field_dictionary:
        use_field_dictionary.update(field_dictionary)
    if not getattr(settings, "SEARCH_SKIP_ENROLLMENT_START_DATE_FILTERING", False):
        use_field_dictionary["enrollment_start"] = DateRange(None, datetime.utcnow())

    if 'pagepos' in field_dictionary:
        pagepos_val = field_dictionary['pagepos']
    else:
        pagepos_val = ''
   
    #test l : list, d : detail
    pagepos_val = 'l'

    searcher = SearchEngine.get_search_engine(getattr(settings, "COURSEWARE_INDEX_NAME", "courseware_index"))
    if not searcher:
        raise NoSearchEngineError("No search engine specified in settings.SEARCH_ENGINE")

    results = searcher.search(
        query_string=search_term,
        doc_type="course_info",
        size=size,
        from_=from_,
        # only show when enrollment start IS provided and is before now
        field_dictionary=use_field_dictionary,
        # show if no enrollment end is provided and has not yet been reached
        #filter_dictionary={"enrollment_end": DateRange(datetime.utcnow(), None)},
        filter_dictionary={"enrollment_start": DateRange(None, datetime.utcnow())},
        exclude_dictionary=exclude_dictionary,
        pagepos=pagepos_val,
        classfysub=classfy_val,
        middle_classfysub=middle_classfy_val,
        facet_terms=course_discovery_facets(),
    )

    return results

