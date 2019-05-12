#
# Copyright (c) nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/scancode-toolkit/
# The ScanCode software is licensed under the Apache License version 2.0.
# Data generated with ScanCode require an acknowledgment.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with ScanCode or any ScanCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with ScanCode and provided on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  ScanCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  ScanCode is a free software code scanning tool from nexB Inc. and others.
#  Visit https://github.com/nexB/scancode-toolkit/ for support and download.

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

from array import array
from collections import defaultdict
from math import log

from intbitset import intbitset

from commoncode.dict_utils import sparsify
from licensedcode.tokenize import ngrams


"""
Approximate matching strategies using token and ngram sets and multisets.

This is used as a pre-filter to find candidates rules that have the highest
likeliness of matching a query and to filter rules that could not possibly yield
a valid match. The candidates rules are later for pair-wise matching with the
query. This way either less or no matching is needed.

We collect a subset of rules that could be matched by ranking them and keep the
top candidates. We also filter out rules based on minimum thresholds such as
matched token occurrences or an approximation of the length of a match.

The primary technique is token ids sets and multisets intersections. We use the
a tf-idf and intersection length to compute scores/ranking elements including
ressemblance and containment. This is essentially a traditional IR inverted
index approach.

But we also want to return every matches and not just probabilistic top-ranked
matches based on frequencies as is typically done in a search engine. There2fore
we compute the intersection of the query against every rules. This proves more
efficient than a traditional inverted intersection in part because the queries
are much larger (1000's of tokens) than a traditional search engine query.

Since we use integers to represent tokens, we reduce the problem to integer set
or multisets/bags/counters intersections. Furthermore, we have a finite and
limited number of tokens.

Two techniques are used in sequence: tokens sets and multisets.

Tokens occurrence sets
======================

A tokens occurrence set is represented as an array of bits (aka. a bitmap) where
each bit position corresponds to a token id. The length of each bit array is
therefore equal to the number of unique tokens across all rules. This forms a
term occurrence matrix stored compactly as bitmaps. With about 15K unique tokens
and about 6k rules, we store about 90 millions bits (15K x 6K) for about 10MB
of total storage for this matrix. Computing intersections of bitmaps is fast
even if it needs to be done thousand times for each query and query run.

The length of the intersection of a query and rule bitmap tells us the count of
shared tokens. We can skip rules based on thresholds and we then rank and keep
the top rules.


Tokens ids  multisets aka. frequency counters aka. term vectors
===============================================================

A tokens frequency counter maps a token id to the number of times it shows up in
a text. This is also called a multiset or a bag or counter or a term vector.

Given the subset of ranked candidate rules from the token sets intersection
step, we intersect the query and rule token multisets. For each shared token we
collect the minimum count of a token present in both. We sum these to obtain an
approximation to the number of matchable tokens between the query and rule. This
is an approximation because it does not consider the relative positions of the
tokens so it may be bigger than what will eventually be matched using a sequence
alignment.

This sum is then used for the same filtering and ranking used for the token sets
step: skip if some threshold is not met and rank the candidates.

Finally we return the sorted top candidates.
"""

# Set to True for tracing
TRACE = False
TRACE_DEEP = False
TRACE_CANDIDATES = False


def logger_debug(*args): pass


if TRACE or TRACE_CANDIDATES:
    import logging
    import sys

    logger = logging.getLogger(__name__)
    logging.basicConfig(stream=sys.stdout)
    logger.setLevel(logging.DEBUG)

    def logger_debug(*args):
        return logger.debug(' '.join(isinstance(a, basestring) and a or repr(a) for a in args))


def tids_sets_intersector(qset, iset):
    """
    Return the intersection of a query and index token ids sets.
    """
    return qset & iset


tids_set_counter = len


def multisets_intersector(qmset, imset):
    """
    Return the intersection of a query and index token id bigrams multisets. For
    a key present in both multisets, the intersection value is the smaller of
    the occurence count in the query and rule for this key. Optimized for
    defaultdicts.
    """
    # NOTE: Using a Counter is less efficient
    intersection = defaultdict(int)
    # iterate the smallest of the two sets
    if len(qmset) < len(imset):
        set1, set2 = qmset, imset
    else:
        set1, set2 = imset, qmset

    for key, s1count in set1.items():
        s2count = set2[key]
        intersection[key] = min(s1count, s2count)
    return {k: count for k, count in intersection.items() if count}


def multiset_counter(mset):
    """
    Return the sum of occurences of elements present in a token ids multiset,
    aka. the multiset cardinality.
    """
    return sum(mset.values())


def high_tids_set_subset(tids_set, len_junk):
    """
    Return a subset of a set of token ids that are high tokens.
    """
    return intbitset([i for i in tids_set if i >= len_junk])


def high_multiset_subset(mset, len_junk):
    """
    Return a subset of a multiset of bigrams that contain high tokens.
    """
    return {bigram: count for bigram, count in mset.items() 
            if bigram[0] >= len_junk or bigram[1] >= len_junk}


def build_set_and_mset(token_ids):
    """
    Return a tuple of (tids set, multiset) given a `token_ids` tids sequence.
    NOTES: this is used for indexing
    """
    tids_set = intbitset()
    bigrams_mset = defaultdict(int)
    
    for bigram in ngrams(token_ids, 2):
        bigram = tuple(bigram)
        bigrams_mset[bigram] += 1
        tids_set.update(bigram)
    # OPTIMIZED: sparsify for speed
    sparsify(bigrams_mset)

    return tids_set, bigrams_mset


def build_query_set_and_mset(query_run):
    """
    Return a tuple of (tids set, multiset) given a `query_run`
    """
    mset = defaultdict(int)
    tids_set = intbitset()
    matchables = query_run.matchables
    
    for qpos, bigram in enumerate(ngrams(query_run.tokens, 2), query_run.start):
        if not(qpos in matchables and qpos+1 in matchables):
            continue
        bigram = tuple(bigram)
        mset[bigram] += 1
        tids_set.update(bigram)

    # OPTIMIZED: sparsify for speed
    sparsify(mset)

    return tids_set, mset


def compute_token_idfs(len_rules, tokens_doc_freq_by_tid):
    """
    Return a mapping of {token id -> inverse document frequency} given mapping
    of `tokens_doc_freq_by_tid` counting the number of rules in which a token if
    occurs and the `len_rules` number of rules.
    Note this is a using a sequence as mapping where the key is the sequence index.
    """
    # note: we use a more compact array of floats where the index is a token id.
    # note we perform some smoothing as in sklearn:
    # See https://github.com/scikit-learn/scikit-learn/blob/645d3224182d1dd3723ffbf983172aad07cfeba8/sklearn/feature_extraction/text.py#L1131
    return array('f', (log((len_rules + 1) / (tdf + 1)) + 1
                       for tdf in tokens_doc_freq_by_tid))


def compute_bigram_idfs(len_rules, bigrams_doc_freq_by_tid):
    """
    Return a mapping of {bigram -> inverse document frequency} given a mapping
    of `bigram s_doc_freq_by_tid` counting the number of rules in which a bigram
    occurs and the `len_rules` number of rules.
    """
    # note we perform some smoothing as in sklearn:
    # See https://github.com/scikit-learn/scikit-learn/blob/645d3224182d1dd3723ffbf983172aad07cfeba8/sklearn/feature_extraction/text.py#L1131
    return {big: (log((len_rules + 1) / (bdf + 1)) + 1)
            for big, bdf in bigrams_doc_freq_by_tid.items()}


def compute_high_set_and_mset(tids_set, mset, len_junk):
    """
    Return a tuple of (high tids set, high tids multiset) given a
    tids_set and mset of all token tids and the `len_junk`.
    """
    high_tids_set = high_tids_set_subset(tids_set, len_junk)
    high_mset = high_multiset_subset(mset, len_junk)
    return high_tids_set, high_mset


# FIXME: we should consider existing aho matches when considering candidate
# and not rematch these at all

# FIXME: we should consider more aggressively the thresholds and what a match filters
# would discard when we compute candaites to eventually discard many or all candidates
# we compute too many candidates that may waste time in seq matching for no reason

def compute_candidates(query_run, idx, matchable_rids, top=50):
    """
    Return a ranked list of rule candidates for further matching give a
    `query_run`. Use approximate matching based on token sets ignoring
    positions. Only consider rules that have an rid in a `matchable_rids` rids
    set if provided.

    The ranking is based on a combo of resemblance, containment, length and
    other measures.
    """
    # collect query-side sets used for matching
    qset, qmset = build_query_set_and_mset(query_run)

    len_junk = idx.len_junk

    # perform two steps of ranking:
    # step one with tid sets and step two with tid multisets for refinement

    ############################################################################
    # step 1 is on token id sets:
    ############################################################################
    intersector = tids_sets_intersector
    counter = tids_set_counter
    high_intersection_filter = high_tids_set_subset

    qset_len = counter(qset)
    high_qset = high_intersection_filter(qset, len_junk)
    high_qset_len = counter(high_qset)

    sets_by_rid = idx.sets_by_rid
    unique = True
    idf_by_tid = idx.tokens_idf_by_tid
    tfidf_computer = compute_tfidf_tids_set_score

    candidates = ((None, rid, rule, None) for rid, rule in enumerate(idx.rules_by_rid)
                  if rid in matchable_rids)
    for step in 'sets', 'multisets':
        sortable_candidates = []
        sortable_candidates_append = sortable_candidates.append

        for _, rid, rule, intersection in candidates:
            iset = sets_by_rid[rid]

            rank, inter = compare_token_sets(
                qset,
                qset_len,
                high_qset_len,
                iset,
                intersector,
                counter,
                high_intersection_filter,
                len_junk,
                unique,
                rule,
                tfidf_computer,
                idf_by_tid,
            )

            if rank:
                # With this trick the intersection will be the one from the
                # first step, e.g. a simple set. On the first step, intersection
                # is None and inter is the intersected set. On the second step,
                # intersection is is the intersected set of the first step, so
                # the intersected multiset is ignored.
                inter = intersection or inter
                sortable_candidates_append((rank, rid, rule, inter))

        if not sortable_candidates:
            return sortable_candidates

        # rank and keep only the top candidates
        sortable_candidates.sort(reverse=True)
        candidates = sortable_candidates[:top * 10]

        if TRACE_CANDIDATES and candidates:
            logger_debug('\n\n\ncompute_candidates:', step, 'candidates:', len(candidates))
            for scores, _rid, rule, _inter in candidates[:top * 5]:
                logger_debug(rule)
                logger_debug(scores)
                logger_debug()

        ########################################################################
        # step 2 is on tids multisets
        ########################################################################

        qset = qmset
        intersector = multisets_intersector
        counter = multiset_counter
        high_intersection_filter = high_multiset_subset

        qset_len = counter(qset)
        high_qset = high_intersection_filter(qset, len_junk)
        high_qset_len = counter(high_qset)

        sets_by_rid = idx.msets_by_rid
        unique = False
        idf_by_tid = idx.bigrams_idf_by_bigram
        tfidf_computer = compute_tfidf_mset_score

    ###########################################################################
    # return top and remove sort_order from Schwartzian transform)
    candidates = [(candidate_rule, intersection)
                  for _rank, _rid, candidate_rule, intersection in candidates[:top]]

    if TRACE_CANDIDATES and candidates:
        logger_debug('\n\n\ncompute_candidates: FINAL candidates:', len(candidates))
        for rule, _intersection in candidates:
            logger_debug(rule)

    return candidates


def compare_token_sets(
        qset, qset_len, high_qset_len,
        iset,
        intersector, counter, high_intersection_filter,
        len_junk, unique,
        rule,
        tfidf_computer,
        idf_by_tid):
    """
    Return a score tuple for rank sorting key or (None, None).
    Compare a `qset` query token ids set or multiset with a `iset` index rule
    token ids set or multiset.
    """
    intersection = intersector(qset, iset)
    if not intersection:
        return None, None
    high_intersection = high_intersection_filter(intersection, len_junk)
    if not high_intersection:
        return None, None

    high_matched_length = counter(high_intersection)
    min_high_matched_length = rule.get_min_high_matched_length(unique)

    # need some high match above min high
    if high_matched_length < min_high_matched_length:
        return None, None

    iset_len = rule.get_length(unique)
    matched_length = counter(intersection)
    min_matched_length = rule.get_min_matched_length(unique)

    if matched_length < min_matched_length:
        return None, None

    high_iset_len = rule.get_high_length(unique)

    # Compute ranking elements
    #########################################

    # distance
    distance = iset_len - matched_length
    high_distance = high_iset_len - high_matched_length

    # resemblance and containment
    union_len = qset_len + iset_len - matched_length
    resemblance = matched_length / union_len
    containment = min(1, matched_length / iset_len)

    minimum_coverage = rule.minimum_coverage
    # FIXME: we should not recompute this /100 ... it should be cached
    if minimum_coverage and containment < (minimum_coverage / 100):
        return None, None

    high_union_length = high_qset_len + high_iset_len - high_matched_length
    high_resemblance = high_matched_length / high_union_length
    high_containment = min(1 , high_matched_length / high_iset_len)

    adjusted_containment = high_containment
    low_matched_len = matched_length - high_matched_length
    if low_matched_len > 0:
        low_iset_len = iset_len - high_iset_len
        low_containment = min(1, low_matched_len / (low_iset_len or 0.0000001))
        low_importance = 0.9
        adjusted_containment = (high_containment + (low_containment * low_importance)) / (1 + low_importance)
        adjusted_containment = min(1, adjusted_containment)

    tfidf_score = tfidf_computer(intersection=intersection,
        qset_len=qset_len, idf_by_tid=idf_by_tid)

    high_tfidf_score = tfidf_computer(intersection=high_intersection,
        qset_len=high_qset_len, idf_by_tid=idf_by_tid)

#     tdidf_score = tdidf_score / intersection_len

    score = (
        6 * high_tfidf_score +
        10 * tfidf_score +
        4 * adjusted_containment +
        4 * containment +
        4 * high_containment +
        1 * high_resemblance +
        1 * resemblance
    ) / 30

    if TRACE_CANDIDATES:
        return (
            'score', score,
            'high_tfidf_score', round(high_tfidf_score, 5),
            'tfidf_score', round(tfidf_score, 5),
            'adjusted_containment', round(adjusted_containment, 5),
            'high_containment', round(high_containment, 5),
            'containment', round(containment, 5),
            'high_resemblance', round(resemblance, 5),
            'resemblance', round(resemblance, 5),
            'high_matched_length', high_matched_length,
            '-high_distance', -high_distance,
            '-distance', -distance,
            'matched_length', matched_length,
            'iset_len', iset_len,
            'qset_len', qset_len,
        ), high_intersection

    return (
        score,
        round(high_tfidf_score, 5),
        round(tfidf_score, 5),
        round(adjusted_containment, 5),
        round(high_containment, 5),
        round(containment, 5),
        round(resemblance, 5),
        round(resemblance, 5),
        high_matched_length,
        -high_distance,
        -distance,
        matched_length,
        iset_len,
        qset_len,
    ), high_intersection


def compute_tfidf_tids_set_score(intersection, qset_len, idf_by_tid):
    """
    Return a score as a float for an `intersection` set of matched token ids from
    a query of length `qset_len` and `idf_by_tid` mapping of
    {token id -> idf}
    """
    # TODO: double check that qset_len is the length of unique tokens!!!
    return sum((1 / qset_len) * idf_by_tid[tid] for tid in intersection)


def compute_tfidf_mset_score(intersection, qset_len, idf_by_tid):
    """
    Return a score as a float for an `intersection` multiset of matched token id
    bigrams from a query of length `qset_len` and `idf_by_tid` a mapping
    of {bigram -> idf}
    """
    return sum((tid_count / qset_len) * idf_by_tid[bigram]
               for bigram, tid_count in intersection.items())