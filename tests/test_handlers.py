# -*- coding: utf-8 -*-
import re
import os
import json
import main
import cache
import views
import webapp2
import lxml.etree
import urllib
from models import WikiPage
import unittest2 as unittest
from google.appengine.ext import testbed
from lxml.html import html5parser

from tests.test_auth import OAuthStub


class ContentTypeTest(unittest.TestCase):
    def setUp(self):
        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_datastore_v3_stub()
        self.testbed.init_memcache_stub()
        self.testbed.init_user_stub()
        self.testbed.init_taskqueue_stub()

        self.browser = Browser()

    def tearDown(self):
        self.testbed.deactivate()
        self.browser.logout()

    def test_get_default_content_type(self):
        p = WikiPage.get_by_title(u'Test')
        p.update_content(u'Hello', 0)
        self.browser.get('/Test')
        self.assertEqual('text/html; charset=utf-8', self.browser.res.headers['Content-type'])

    def test_get_json_content_type(self):
        p = WikiPage.get_by_title(u'Test')
        p.update_content(u'Hello', 0)
        self.browser.get('/Test?_type=json')
        self.assertEqual('application/json; charset=utf-8', self.browser.res.headers['Content-type'])

    def test_get_custom_content_type(self):
        p = WikiPage.get_by_title(u'Test')
        p.update_content(u'.content-type text/plain\nHello', 0)
        self.browser.get('/Test')
        self.assertEqual('text/plain; charset=utf-8', self.browser.res.headers['Content-type'])
        self.assertEqual('Hello', self.browser.res.body)

    def test_view_should_override_custom_content_type(self):
        p = WikiPage.get_by_title(u'Test')
        p.update_content(u'.content-type text/plain\nHello', 0)
        self.browser.get('/Test?view=edit')
        self.assertEqual('text/html; charset=utf-8', self.browser.res.headers['Content-type'])

    def test_should_not_restrict_read_access_to_custom_content_type(self):
        p = WikiPage.get_by_title(u'Test')
        self.assertRaises(ValueError, p.update_content, u'.read blah\n.content-type text/plain\nHello', 0)


class PageHandlerTest(unittest.TestCase):
    def setUp(self):
        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_datastore_v3_stub()
        self.testbed.init_memcache_stub()
        self.testbed.init_taskqueue_stub()
        self.testbed.init_user_stub()
        self.oauth_stub = OAuthStub(self.testbed, logout=True)

        self.fixtures = [
            [u'Home', u'Home'],
            [u'A', u'Goto [[Home]]'],
            [u'Post A', u'.pub\nHello'],
            [u'Post B', u'.pub\nHello'],
            [u'Post C', u'.pub BBS\nHello'],
            [u'BBS', u'.schema Blog\nBlog'],
        ]

        for title, body in self.fixtures:
            page = WikiPage.get_by_title(title)
            page.update_content(body, 0, None)

        self.browser = Browser()

    def tearDown(self):
        self.testbed.deactivate()
        self.browser.logout()

    def test_get_sp_chages(self):
        for _ in range(2):
            self.browser.get('/sp.changes')
            links = self.browser.query('.//table//a')
            link_texts = [link.text for link in links]
            self.assertEqual([u'BBS', u'Post C', u'Post B', u'Post A', u'A', u'Home'], link_texts)

    def test_get_sp_index(self):
        for _ in range(2):
            self.browser.get('/sp.index')
            links = self.browser.query('.//table//a')
            link_texts = [link.text for link in links]
            self.assertEqual([u'A', u'BBS', u'Home', u'Post A', u'Post B', u'Post C'], link_texts)

    def test_get_sp_posts(self):
        for _ in range(2):
            self.browser.get('/sp.posts')
            links = self.browser.query('.//table//a')
            link_texts = [link.text for link in links]
            self.assertEqual([u'Post B', u'Post A'], link_texts)

    def test_get_posts_to_page(self):
        for _ in range(2):
            self.browser.get('/BBS')
            links = self.browser.query('.//table//a')
            link_texts = [link.text for link in links]
            self.assertEqual([u'Post C'], link_texts)

    def test_get_wikipage(self):
        for _ in range(2):
            self.browser.get('/A')
            links = self.browser.query('.//article//a[@class=\'wikipage\']')
            link_texts = [link.text for link in links]
            self.assertEqual(['Home'], link_texts)

    def test_post_in_json(self):
        self.browser.logout()
        self.oauth_stub.login('jh@gmail.com', 'jh')

        self.browser.post('/New_page?_type=json', 'body=[[Link!]]&revision=0')
        if self.browser.res.status_code % 100 == 3:
            self.browser.get(self.browser.res.location)
        self.assertEqual(self.browser.res.status_code, 200)
        self.assertEqual('application/json; charset=utf-8', self.browser.res.headers['Content-type'])

    def test_put_without_permission(self):
        self.browser.post('/New_page?_method=PUT', 'body=[[Link!]]&revision=0')
        self.assertEqual(403, self.browser.res.status_code)

    def test_put_in_json(self):
        self.browser.logout()
        self.oauth_stub.login('jh@gmail.com', 'jh')

        self.browser.post('/New_page?_method=PUT&_type=json', 'body=[[Link!]]&revision=0')
        if self.browser.res.status_code % 100 == 3:
            self.browser.get(self.browser.res.location)
        self.assertEqual(self.browser.res.status_code, 200)
        self.assertEqual('application/json; charset=utf-8', self.browser.res.headers['Content-type'])

    def test_new_page_should_be_shown_in_sp_changes(self):
        self.browser.login('ak@gmail.com', 'ak')
        self.browser.post('/New_page?_method=PUT', 'body=[[Link!]]&revision=0')

        for _ in range(2):
            self.browser.get('/sp.changes')
            links = self.browser.query('.//table//a')
            link_texts = [link.text for link in links]
            self.assertEqual([u'New page', u'BBS', u'Post C', u'Post B', u'Post A', u'A', u'Home'],
                             link_texts)

    def test_new_page_should_be_shown_in_sp_index(self):
        self.browser.login('ak@gmail.com', 'ak')
        self.browser.post('/New_page?_method=PUT', 'body=[[Link!]]&revision=0')

        for _ in range(2):
            self.browser.get('/sp.index')
            links = self.browser.query('.//table//a')
            link_texts = [link.text for link in links]
            self.assertEqual([u'A', u'BBS', u'Home', u'New page', u'Post A', u'Post B', u'Post C'],
                             link_texts)

    def test_redirect_metadata(self):
        page = WikiPage.get_by_title(u'Hi')
        page.update_content(u'.redirect Hello World', 0)

        self.browser.get('/Hi', follow_redir=False)
        self.assertEqual(303, self.browser.res.status_code)
        self.assertEqual('http://localhost/Hello_World',
                         self.browser.res.location)

    def test_delete_page_without_permission(self):
        self.browser.login('ak@gmail.com', 'ak', is_admin=False)
        self.browser.post('/New_page?_method=PUT', 'body=[[Link!]]&revision=0')
        self.browser.post('/New_page?_method=DELETE')

        self.assertEqual(403, self.browser.res.status_code)

        self.browser.get('/sp.index')
        links = self.browser.query('.//table//a')
        link_texts = [link.text for link in links]
        self.assertTrue(u'New page' in link_texts)


class RevisionTest(unittest.TestCase):
    def setUp(self):
        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_datastore_v3_stub()
        self.testbed.init_memcache_stub()
        self.testbed.init_user_stub()
        self.testbed.init_taskqueue_stub()
        self.parser = html5parser.HTMLParser(strict=True)
        self.browser = Browser()

    def tearDown(self):
        self.testbed.deactivate()

    def test_rev(self):
        page = WikiPage.get_by_title(u'A')
        page.update_content(u'Hello', 0)
        page.update_content(u'Hello there', 1)

        self.browser.get('/A?rev=1')
        self.assertEqual(200, self.browser.res.status_code)

        self.browser.get('/A?_type=txt&rev=1')
        self.assertEqual(200, self.browser.res.status_code)

        self.browser.get('/A?view=bodyonly&rev=1')
        self.assertEqual(200, self.browser.res.status_code)

    def test_rev_param(self):
        page = WikiPage.get_by_title(u'A')
        page.update_content(u'Hello', 0)
        page.update_content(u'Hello there', 1)

        self.browser.get('/A?_type=txt&rev=1')
        self.assertEqual(u'Hello', self.browser.res.body)

        self.browser.get('/A?_type=txt&rev=2')
        self.assertEqual(u'Hello there', self.browser.res.body)

        self.browser.get('/A?_type=txt&rev=latest')
        self.assertEqual(u'Hello there', self.browser.res.body)

    def test_rev_acl(self):
        self.browser.login('a@x.com', 'a')
        page = WikiPage.get_by_title(u'A')
        page.update_content(u'Hello', 0)
        page.update_content(u'.read a@x.com\nHello there', 1)

        self.browser.get('/A?_type=txt&rev=1')
        self.assertEqual(200, self.browser.res.status_code)
        self.browser.get('/A?_type=txt&rev=2')
        self.assertEqual(200, self.browser.res.status_code)

        self.browser.logout()
        self.browser.get('/A?_type=txt&rev=1')
        self.assertEqual(200, self.browser.res.status_code)
        self.browser.get('/A?_type=txt&rev=2')
        self.assertEqual(403, self.browser.res.status_code)


class HTML5ValidationTest(unittest.TestCase):
    def setUp(self):
        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_datastore_v3_stub()
        self.testbed.init_memcache_stub()
        self.testbed.init_user_stub()
        user_stub = self.testbed._test_stub_map.GetStub(testbed.USER_SERVICE_NAME)
        user_stub.SetOAuthUser(email=None) # no OAuth login

        self.testbed.init_taskqueue_stub()
        self.parser = html5parser.HTMLParser(strict=True)
        self.browser = Browser()

        self.fixtures = [
            [u'Home', u'Goto [[한글 제목]]'],
            [u'A', u'Goto [[Home]]'],
            [u'한글 제목', u'Goto [[Home]]'],
            [u'Presentation', u'.pt\nHello'],
            [u'Blog post', u'.pub\nHello'],
        ]

        for title, body in self.fixtures:
            page = WikiPage.get_by_title(title)
            page.update_content(body, 0, None)
            # update again to create revisions
            page.update_content(body + u'!', 1, None)

    def tearDown(self):
        self.testbed.deactivate()
        self.browser.logout()

    def test_normal_pages(self):
        for title, _ in self.fixtures:
            path = WikiPage.title_to_path(title)
            self._validate('/%s' % path, 'html')
            self._validate('/%s?rev=1' % path, 'html')
            self._validate('/%s?view=bodyonly' % path, 'html')
            self._validate('/%s?view=edit' % path, 'html')

    def test_special_pages(self):
        def validate():
            self._validate('/sp.changes', 'html')
            self._validate('/sp.changes?_type=atom', 'xml')

            self._validate('/sp.index', 'html')
            self._validate('/sp.index?_type=atom', 'xml')

            self._validate('/sp.search?_type=json&view=opensearch', 'json')

            self._validate('/="Home"', 'html')
            self._validate('/="Home"?view=bodyonly', 'html')
            self._validate('/="Home"?_type=json', 'json')

            self._validate('/sp.titles?_type=json', 'json')

            self._validate('/sp.posts', 'html')
            self._validate('/sp.posts?_type=atom', 'xml')

            self._validate('/sp.randomly_update_related_pages', 'text')
            self._validate('/sp.randomly_update_related_pages?recent=1', 'text')

        self.browser.login('user@example.com', 'ak', is_admin=False)
        validate()
        self._validate('/sp.preferences', 'html', 200)

        self.browser.login('user@example.com', 'ak', is_admin=True)
        validate()
        self._validate('/sp.preferences', 'html', 200)

        self.browser.logout()
        validate()
        self._validate('/sp.preferences', 'html', 403)

    def _validate(self, url, validator, status_code=200):
        req = webapp2.Request.blank(url)
        res = req.get_response(main.app)

        self.assertEqual(status_code, res.status_code)

        if validator == 'html':
            self.assertEqual('text/html', res.content_type)
            html5parser.fromstring(res.body, parser=self.parser)
        elif validator == 'xml':
            self.assertEqual('text/xml', res.content_type)
            lxml.etree.XML(res.body)
        elif validator == 'json':
            self.assertEqual('application/json', res.content_type)
            json.loads(res.body)
        elif validator == 'text':
            self.assertEqual('text/plain', res.content_type)


# TODO: Complete this test cases and remove redundent tests
class PageResourceTest(unittest.TestCase):
    def setUp(self):
        cache.prc.flush_all()

        self.testbed = testbed.Testbed()
        self.testbed.activate()
        self.testbed.init_datastore_v3_stub()
        self.testbed.init_memcache_stub()
        self.testbed.init_taskqueue_stub()
        self.testbed.init_user_stub()
        self.oauth_stub = OAuthStub(self.testbed, logout=True)
        self.browser = Browser()

    def tearDown(self):
        self.testbed.deactivate()
        self.browser.logout()

    def test_create_new_page(self):
        self.browser.login('ak@gmail.com', 'ak')

        # GET edit form of "New page"
        self.browser.get('/New_page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # PUT "New page"
        self.browser.submit(".//form[@class='editform']", {'body': 'Hello', 'revision': '0', 'preview': '0'})
        self.assertEqual(303, self.browser.res.status_code)
        self.assertEqual('http://localhost/New_page', self.browser.res.headers['Location'])

        # Check
        page = WikiPage.get_by_title(u'New page')
        self.assertEqual(u'Hello', page.body)

    def test_update_existing_page(self):
        self.browser.login('ak@gmail.com', 'ak')

        # Create page
        page = WikiPage.get_by_title(u'New page')
        page.update_content(u'Hello', 0)

        # GET edit form of "New page"
        self.browser.get('/New_page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # PUT "New page"
        self.browser.submit(".//form[@class='editform']", {'body': 'Hello there', 'revision': '1', 'preview': '0'})
        self.assertEqual(303, self.browser.res.status_code)
        self.assertEqual('http://localhost/New_page', self.browser.res.headers['Location'])

        # Check
        page = WikiPage.get_by_title(u'New page')
        self.assertEqual(u'Hello there', page.body)
        self.assertEqual(2, page.revision)

    def test_preview(self):
        self.browser.login('ak@gmail.com', 'ak')

        # Create page
        page = WikiPage.get_by_title(u'New page')
        page.update_content(u'Hello', 0)

        # GET edit form of "New page"
        self.browser.get('/New_page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # PUT "New page" with preview="1"
        self.browser.submit(".//form[@class='editform']", {'body': 'Hello there', 'revision': '1', 'preview': '1'})
        self.assertEqual(200, self.browser.res.status_code)
        self.assertEqual(u'<!DOCTYPE html><html lang="ko">\n<head><meta charset="utf-8"><title>New page</title></head>\n<body><div class="wrap">\n<p>Hello there</p>\n</div></body>\n</html>', self.browser.res.body)

        # Check if not changed
        page = WikiPage.get_by_title(u'New page')
        self.assertEqual(u'Hello', page.body)
        self.assertEqual(1, page.revision)

    def test_append_to_non_existing_page(self):
        self.browser.login('ak@gmail.com', 'ak')

        # GET edit form of "New page"
        self.browser.get('/New_page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # POST to "New page"
        self.browser.submit(".//form[@class='appendform']", {'body': 'Hello'})
        self.assertEqual(303, self.browser.res.status_code)
        self.assertEqual('http://localhost/New_page', self.browser.res.headers['Location'])

        # Check
        page = WikiPage.get_by_title(u'New page')
        self.assertEqual(u'Hello', page.body)

    def test_append_to_existing_page(self):
        self.browser.login('ak@gmail.com', 'ak')

        # Create page
        page = WikiPage.get_by_title(u'New page')
        page.update_content(u'Hello', 0)

        # GET edit form of "New page"
        self.browser.get('/New_page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # POST to "New page"
        self.browser.submit(".//form[@class='appendform']", {'body': ' there'})
        self.assertEqual(303, self.browser.res.status_code)
        self.assertEqual('http://localhost/New_page', self.browser.res.headers['Location'])

        # Check
        page = WikiPage.get_by_title(u'New page')
        self.assertEqual(u'Hello there', page.body)
        self.assertEqual(2, page.revision)

    def test_delete_non_existing_page(self):
        self.browser.login('ak@gmail.com', 'ak', is_admin=True)

        # GET edit form of "New page"
        self.browser.get('/New_page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # DELETE "New page"
        self.browser.submit(".//form[@class='deleteform']")
        self.assertEqual(204, self.browser.res.status_code)

        # Check
        page = WikiPage.get_by_title(u'New page')
        self.assertEqual(u'', page.body)
        self.assertEqual(0, page.revision)

    def test_delete_existing_page(self):
        self.browser.login('ak@gmail.com', 'ak', is_admin=True)

        # Create page
        page = WikiPage.get_by_title(u'New page')
        page.update_content(u'Hello', 0)

        # GET edit form of "New page"
        self.browser.get('/New_page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # DELETE "New page"
        self.browser.submit(".//form[@class='deleteform']")
        self.assertEqual(204, self.browser.res.status_code)

        # Check
        page = WikiPage.get_by_title(u'New page')
        self.assertEqual(u'', page.body)
        self.assertEqual(0, page.revision)

    def test_redirect_in_absolute_path(self):
        self.browser.login('ak@gmail.com', 'ak')

        # GET edit form of "New/page"
        self.browser.get('/New/page')
        self.browser.get(self.browser.query_link(".//a[@id='edit']"))

        # PUT "New page"
        self.browser.submit(".//form[@class='editform']", {'body': 'Hello', 'revision': '0', 'preview': '0'})
        self.assertEqual(303, self.browser.res.status_code)
        self.assertEqual('http://localhost/New/page', self.browser.res.headers['Location'])

    def test_root(self):
        self.browser.login('ak@gmail.com', 'ak')
        self.browser.get('/', follow_redir=False)
        self.assertEqual('http://localhost/Home', self.browser.res.headers['location'])
        self.assertEqual('text/html; charset=utf-8', self.browser.res.headers['content-type'])

    def test_root_with_querystring(self):
        self.browser.login('ak@gmail.com', 'ak')
        self.browser.get('/?_type=txt', follow_redir=False)
        self.assertEqual('http://localhost/Home?_type=txt', self.browser.res.headers['location'])

    def test_response_representations(self):
        # Create page
        page = WikiPage.get_by_title(u'New page')
        page.update_content(u'Hello', 0)

        self.browser.get('/New page')
        self.assertEqual('text/html; charset=utf-8', self.browser.res.headers['content-type'])
        self.browser.get('/New page?_type=txt')
        self.assertEqual('text/plain; charset=utf-8', self.browser.res.headers['content-type'])
        self.browser.get('/New page?_type=json')
        self.assertEqual('application/json; charset=utf-8', self.browser.res.headers['content-type'])

    def test_revision_history(self):
        self.browser.get('/New page?rev=list')
        self.assertEqual('text/html; charset=utf-8', self.browser.res.headers['content-type'])
        self.browser.get('/New page?rev=list&_type=json')
        self.assertEqual('application/json; charset=utf-8', self.browser.res.headers['content-type'])


class Browser(object):
    def __init__(self):
        self.parser = html5parser.HTMLParser(strict=True)
        self.res = None
        self.tree = None

    def get(self, url, follow_redir=True):
        req = webapp2.Request.blank(url)
        self.res = req.get_response(main.app)
        if len(self.res.body) > 0 and self.res.headers['content-type'].split(';')[0].strip() == 'text/html':
            self.tree = html5parser.fromstring(self.res.body, parser=self.parser)
        if follow_redir and self.res.status_code in [301, 302, 303, 304, 307] and 'location' in self.res.headers:
            self.get(self.res.headers['location'][16:], follow_redir=True)

    def post(self, url, content=''):
        req = webapp2.Request.blank(url)
        req.method = 'POST'
        req.body = content
        self.res = req.get_response(main.app)

    def query(self, path):
        return self._query(self.tree, path)

    def query_link(self, path):
        return self.query(path)[0].attrib['href']

    def submit(self, form_path, fields={}):
        action_link = self.query(form_path)[0].attrib['action']
        supplied_names = set(fields.keys())
        form_names = set(self._query_formfields(form_path))
        if supplied_names != form_names:
            raise ValueError('Names do not match. Form: [%s], Supplied: [%s]' % (','.join(form_names), ','.join(supplied_names)))

        self.post(action_link, urllib.urlencode(fields))

    def login(self, email, user_id, is_admin=False):
        os.environ['USER_EMAIL'] = email or ''
        os.environ['USER_ID'] = user_id or ''
        os.environ['USER_IS_ADMIN'] = '1' if is_admin else '0'

    def logout(self):
        self.login(None, None)

    def _query_formfields(self, path):
        form = self.query(path)[0]
        field_path = [
            ".//input[@type='text']",
            ".//input[@type='hidden']",
            ".//input[@type='radio']",
            ".//input[@type='checkbox']",
            ".//textarea",
            ".//select",
        ]
        elements = reduce(lambda a, b: a + b, [self._query(form, p) for p in field_path], [])
        names = [e.attrib['name'] for e in elements]
        return sorted(names)

    def _query(self, element, path):
        path = re.sub(r'/(\w+\d?)', r'/html:\1', path)
        return element.findall(path, namespaces={'html': 'http://www.w3.org/1999/xhtml'})
