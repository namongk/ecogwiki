# -*- coding: utf-8 -*-

import re
import operator
import pyparsing as p
from collections import OrderedDict


P_EXP = ur'(?P<sign>[+-])(?P<title>.+?)(?=\s$|\s[+-])'


def parse_expression(exp):
    """parse related page search expression"""
    exp = exp.strip() + ' '
    positives = []
    negatives = []

    for m in re.finditer(P_EXP, exp):
        sign = m.group('sign')
        title = m.group('title')
        if sign == '+':
            positives.append(title)
        else:
            negatives.append(title)

    return {
        'pos': positives,
        'neg': negatives,
    }


def evaluate(positives, negatives):
    """evaluate related page search expression"""
    scoretable = {}
    keys = positives.keys() + negatives.keys()
    length = len(keys)

    # calc positives
    for scores in positives.values():
        for title, score in scores.items():
            if title in keys:
                continue
            if title not in scoretable:
                scoretable[title] = 0.0
            scoretable[title] += score / length

    # calc negatives
    for scores in negatives.values():
        for title, score in scores.items():
            if title in keys:
                continue
            if title not in scoretable:
                scoretable[title] = 0.0
            scoretable[title] -= score / length

    # descending by score
    sorted_tuples = sorted(scoretable.iteritems(),
                           key=operator.itemgetter(1),
                           reverse=True)

    return OrderedDict(sorted_tuples)


# Wikiquery grammar
identifier = p.Regex(r'([a-zA-Z_][.0-9a-zA-Z_]*)')
double_quote_str = p.dblQuotedString.setParseAction(p.removeQuotes)

page_query_expr = p.Forward()
attr_expr = p.Forward()

expr = page_query_expr + p.Optional(p.Suppress('>') + attr_expr)
expr.setParseAction(lambda x: x if len(x) == 2 else [x[0], ['name']])

page_query_term = p.Group(p.Optional(identifier + p.Suppress(':')) + double_quote_str)
page_query_term.setParseAction(lambda x: x if len(x[0]) == 2 else [['name', x[0][0]]])
page_query_expr << p.operatorPrecedence(page_query_term, [
    (p.Literal('*'), 2, p.opAssoc.LEFT),
    (p.Literal('+'), 2, p.opAssoc.LEFT),
])

attr_expr << p.Group(p.delimitedList(identifier))


def parse_wikiquery(q):
    return expr.parseString(q).asList()
