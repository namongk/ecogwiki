# -*- coding: utf-8 -*-
import cache
import schema
import unittest
from google.appengine.ext import testbed
from models import WikiPage


class SchemaPathTest(unittest.TestCase):
    def test_humane_itemtype(self):
        self.assertEqual('Book', schema.humane_item('Book'))
        self.assertEqual('Creative work', schema.humane_item('CreativeWork'))

    def test_humane_property(self):
        self.assertEqual('Publications',
                         schema.humane_property('Book', 'datePublished', True))
        self.assertEqual('Published date',
                         schema.humane_property('Book', 'datePublished', False))

    def test_itemtype_path(self):
        self.assertEqual('Thing/',
                         schema.get_itemtype_path('Thing'))
        self.assertEqual('Thing/CreativeWork/Article/',
                         schema.get_itemtype_path('Article'))

    def test_every_itemtype_should_have_a_parent_except_for_root(self):
        for item in schema.SUPPORTED_SCHEMA.keys():
            self.assertEqual('Thing/', schema.get_itemtype_path(item)[:6])


class SchemaDataTest(unittest.TestCase):
    def setUp(self):
        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_datastore_v3_stub()
        self.testbed.init_memcache_stub()
        cache.prc.flush_all()

    def tearDown(self):
        self.testbed.deactivate()

    def test_default_data(self):
        page = WikiPage.get_by_title(u'Hello')
        page.update_content(u'Hello', 0, '')
        self.assertEqual(u'Hello', page.data['name'])
        self.assertEqual(u'Article', page.data['schema'])

    def test_author_and_isbn(self):
        page = WikiPage.get_by_title(u'Hello')
        page.update_content(u'.schema Book\n[[author::AK]]\n{{isbn::123456789}}', 0, '')
        self.assertEqual(u'Book', page.data['schema'])
        self.assertEqual(u'AK', page.data['author'])
        self.assertEqual(u'123456789', page.data['isbn'])
