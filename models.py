# -*- coding: utf-8 -*-
import re
import yaml
import main
import cache
import random
import schema
import search
import hashlib
import logging
import urllib2
import markdown
import operator
from bzrlib.merge3 import Merge3
from lxml.html.clean import Cleaner
from collections import OrderedDict
from google.appengine.ext import ndb
from google.appengine.api import users
from google.appengine.api import oauth
from datetime import datetime, timedelta
from google.appengine.ext import deferred
from markdown.extensions.def_list import DefListExtension
from markdown.extensions.attr_list import AttrListExtension
from markdownext import md_url, md_wikilink, md_itemprop, md_mathjax, md_strikethrough


logging.getLogger().setLevel(logging.DEBUG)


class PageOperationMixin(object):
    re_img = re.compile(ur'<p><img( .+? )/></p>')
    re_metadata = re.compile(ur'^\.([^\s]+)(\s+(.+))?$')
    re_data = re.compile(ur'({{|\[\[)(?P<name>[^\]}]+)::(?P<value>[^\]}]+)(}}|\]\])')
    re_yaml_schema = re.compile(ur'(?:\s{4}|\t)#!yaml/schema[\n\r]+(((?:\s{4}|\t).+[\n\r]+?)+)')
    re_conflicted = re.compile(ur'<<<<<<<.+=======.+>>>>>>>', re.DOTALL)
    re_special_titles_years = re.compile(ur'^(10000|\d{1,4})( BCE)?$')
    re_special_titles_dates = re.compile(ur'^((?P<month>January|February|March|'
                                         ur'April|May|June|July|August|'
                                         ur'September|October|November|'
                                         ur'December)( (?P<date>[0123]?\d))?)$')

    @property
    def rendered_data(self):
        data = [(n, v, schema.humane_property(self.itemtype, n, False))
                for n, v in self.data.items()
                if n not in ['schema', 'inlinks', 'outlinks']]

        if len(data) == 1:
            # only name and schema?
            return ''

        html = [
            u'<div class="structured-data">',
            u'<h1>Structured data</h1>',
            u'<dl>',
        ]

        data = sorted(data, key=operator.itemgetter(2))

        for name, value, humane_name in data:
            html.append(u'<dt class="key key-%s">%s</dt>' % (name, humane_name))
            if type(value) == list:
                for v in value:
                    html.append(u'<dd class="value value-%s">%s</dd>' % (name, self._render_data_item(name, v)))
            else:
                html.append(u'<dd class="value value-%s">%s</dd>' % (name, self._render_data_item(name, value)))
        html.append(u'</dl></div>')
        return '\n'.join(html)

    def _render_data_item(self, name, value):
        if self._is_schema_item_link(name):
            return u'<span itemprop="%s">%s</span>' % (name, md_wikilink.render_wikilink(value))
        else:
            return u'<span itemprop="%s">%s</span>' % (name, value)

    @property
    def rendered_body(self):
        # body
        body_parts = [PageOperationMixin.remove_metadata(self.body)]

        # incoming links
        if len(self.inlinks) > 0:
            lines = [u'# Incoming Links']
            for rel, links in self.inlinks.items():
                itemtype, rel = rel.split('/')
                humane_rel = schema.humane_property(itemtype, rel, True)
                lines.append(u'## %s' % humane_rel)

                # remove dups and sort
                links = list(set(links))
                links.sort()

                lines += [u'* [[%s]]' % title for title in links]
            body_parts.append(u'\n'.join(lines))

        # related links
        related_links = self.related_links_by_score
        if len(related_links) > 0:
            lines = [u'# Suggested Pages']
            lines += [u'* {{.score::%.3f}} [[%s]]\n{.noli}' % (score, title)
                      for title, score in related_links.items()[:10]]
            body_parts.append(u'\n'.join(lines))

        # other posts
        if self.older_title or self.newer_title:
            lines = [u'# Other Posts']
            if self.newer_title:
                lines.append(u'* {{.newer::newer}} [[%s]]\n{.noli}' % self.newer_title)
            if self.older_title:
                lines.append(u'* {{.older::older}} [[%s]]\n{.noli}' % self.older_title)
            body_parts.append(u'\n'.join(lines))

        # remove yaml/schema block
        joined = u'\n'.join(body_parts)
        joined = re.sub(PageOperationMixin.re_yaml_schema, u'\n', joined)

        # render to html
        rendered = md.convert(joined)

        # add table of contents
        rendered = TocGenerator(rendered).add_toc()

        # add class for embedded image
        rendered = PageOperationMixin.re_img.sub(u'<p class="img-container"><img \\1/></p>', rendered)

        # add structured data block
        rendered = self.rendered_data + rendered

        # sanitize
        if rendered:
            cleaner = Cleaner(safe_attrs_only=False)
            cleaner.host_whitelist = (
                'www.youtube.com',
                'player.vimeo.com',
            )
            rendered = cleaner.clean_html(rendered)

            # remove div wrapper if there is one
            if rendered.startswith('<div>'):
                rendered = rendered[5:-6]

        return rendered

    @property
    def absolute_url(self):
        return u'/%s' % WikiPage.title_to_path(self.title)

    @property
    def absolute_newer_url(self):
        return u'/%s' % WikiPage.title_to_path(self.newer_title)

    @property
    def absolute_older_url(self):
        return u'/%s' % WikiPage.title_to_path(self.older_title)

    @property
    def data(self):
        data = PageOperationMixin.parse_data(self.title, self.itemtype, self.body)

        for rel, links in self.inlinks.items():
            if not rel.endswith('/relatedTo'):
                continue
            if 'inlinks' not in data:
                data['inlinks'] = []
            data['inlinks'] += links

        for rel, links in self.outlinks.items():
            if not rel.endswith('/relatedTo'):
                continue
            if 'outlinks' not in data:
                data['outlinks'] = []
            data['outlinks'] += links

        return data

    @property
    def metadata(self):
        return PageOperationMixin.parse_metadata(self.body)

    def can_read(self, user, default_acl=None):
        if default_acl is None:
            default_acl = PageOperationMixin.get_default_permission()

        acl = self.acl_read.split(',') if self.acl_read is not None and len(self.acl_read) != 0 else []
        if len(acl) == 0:
            acl = default_acl['read']

        if u'all' in acl or len(acl) == 0:
            return True
        elif u'login' in acl and user is not None:
            return True
        elif user is not None and (user.email() in acl or user.email() in self.acl_write.split(',')):
            return True
        elif is_admin_user(user):
            return True
        else:
            return False

    def can_write(self, user, default_acl=None):
        if default_acl is None:
            default_acl = PageOperationMixin.get_default_permission()

        acl = self.acl_write.split(',') if self.acl_write is not None and len(self.acl_write) != 0 else []
        if len(acl) == 0:
            acl = default_acl['write']

        if (not self.can_read(user, default_acl)) and (user is None or user.email() not in acl):
            return False
        elif 'all' in acl:
            return True
        elif (len(acl) == 0 or u'login' in acl) and user is not None:
            return True
        elif user is not None and user.email() in acl:
            return True
        elif is_admin_user(user):
            return True
        else:
            return False

    @property
    def itemtype(self):
        if 'schema' in self.metadata:
            return self.metadata['schema']
        else:
            return u'Article'

    @property
    def itemtype_url(self):
        return 'http://schema.org/%s' % self.itemtype

    @property
    def related_links_by_score(self):
        sorted_tuples = sorted(self.related_links.iteritems(),
                               key=operator.itemgetter(1),
                               reverse=True)
        return OrderedDict(sorted_tuples)

    @property
    def related_links_by_title(self):
        sorted_tuples = sorted(self.related_links.iteritems(),
                               key=operator.itemgetter(0))
        return OrderedDict(sorted_tuples)

    @property
    def special_sections(self):
        ss = {}

        if self._check_special_titles_years():
            ss[u'years'] = self._special_titles_years()
        elif self._check_special_titles_dates():
            ss[u'dates'] = self._special_titles_dates()

        return ss

    @property
    def hashbangs(self):
        return PageOperationMixin.extract_hashbangs(self.rendered_body)

    def _check_special_titles_years(self):
        return (
            self.title != '0' and
            re.match(PageOperationMixin.re_special_titles_years, self.title)
        )

    def _check_special_titles_dates(self):
        return (
            re.match(PageOperationMixin.re_special_titles_dates, self.title)
        )

    def _special_titles_years(self):
        ss = {}

        # years: list year titles
        if self.title.endswith(' BCE'):
            cur_year = -int(self.title[:-4]) + 1
        else:
            cur_year = int(self.title)

        years = range(cur_year - 3, cur_year + 4)
        year_titles = []
        for year in years:
            if year < 1:
                year_titles.append(str(abs(year - 1)) + u' BCE')
            else:
                year_titles.append(str(year))

        ss[u'title'] = 'Years'
        ss[u'years'] = year_titles
        ss[u'cur_year'] = str(cur_year)
        return ss

    def _special_titles_dates(self):
        ss = {}

        # dates: list of dates in month
        month_names = ['January', 'February', 'March', 'April', 'May', 'June',
                       'July', 'August', 'September', 'October', 'November',
                       'December']
        m = re.match(WikiPage.re_special_titles_dates, self.title)
        month = m.group('month')
        max_date = 31
        if month == 'February':
            max_date = 29
        elif month in ('April', 'June', 'September', 'November'):
            max_date = 30
        ss[u'title'] = month
        ss[u'month'] = month
        ss[u'prev_month'] = month_names[month_names.index(month) - 1]
        ss[u'next_month'] = month_names[(month_names.index(month) + 1) %
                                        len(month_names)]
        if m.group('date'):
            ss[u'cur_date'] = int(m.group('date'), 10)

        ss[u'dates'] = range(1, max_date + 1)
        return ss

    def _is_schema_item_link(self, name):
        if name in ['name', 'schema', 'inlinks', 'outlinks']:
            return False
        elif self.itemtype == 'Book' and name in ['isbn']:
            return False
        else:
            return True

    @staticmethod
    def get_default_permission():
        try:
            return WikiPage.get_config()['service']['default_permissions']
        except KeyError:
            return main.DEFAULT_CONFIG['service']['default_permissions']

    @staticmethod
    def parse_data(title, itemtype, body):
        matches = {
            'name': title,
            'schema': schema.get_itemtype_path(itemtype)
        }

        # parse data in yaml/schema section
        m = re.search(PageOperationMixin.re_yaml_schema, body)
        if m:
            parsed_yaml = yaml.load(m.group(1))
            if type(parsed_yaml) != dict:
                raise ValueError('YAML must be a dictionary')

            for name, value in parsed_yaml.items():
                if name in matches:
                    if type(matches[name]) != list:
                        matches[name] = [matches[name]]
                    if type(value) == list:
                        matches[name] += value
                    else:
                        matches[name].append(value)
                else:
                    matches[name] = value

        # parse data embedded in body text
        for m in re.finditer(WikiPage.re_data, body):
            name = m.group('name')
            value = m.group('value')
            if name in matches:
                if type(matches[name]) != list:
                    matches[name] = [matches[name]]
                matches[name].append(value)
            else:
                matches[name] = value
        return matches

    @staticmethod
    def parse_metadata(body):
        matches = []
        for line in body.split(u'\n'):
            m = re.match(WikiPage.re_metadata, line.strip())
            if m:
                matches.append(m)
            else:
                break

        metadata = {
            'content-type': 'text/x-markdown',
            'schema': 'Article',
        }

        for m in matches:
            key = m.group(1).strip()
            value = m.group(3)
            if value is not None:
                value = value.strip()
            metadata[key] = value

        return metadata

    @staticmethod
    def remove_metadata(body):
        rest = []
        lines = iter(body.split(u'\n'))

        for line in lines:
            m = re.match(WikiPage.re_metadata, line.strip())
            if m is None:
                rest.append(line)
                break

        rest += list(lines)
        return u'\n'.join(rest)

    @staticmethod
    def extract_hashbangs(html):
        matches = re.findall(ur'<code>#!(.+?)[\n;]', html)
        if re.match(ur'.*(\\\(.+\\\)|\$\$.+\$\$)', html, re.DOTALL):
            matches.append('mathjax')
        return matches

    def make_description(self, max_length=200):
        # remove yaml/schema block and metadata
        body = re.sub(PageOperationMixin.re_yaml_schema, u'\n', self.body)
        body = PageOperationMixin.remove_metadata(body).strip()

        # try newline
        index = body.find(u'\n')
        if index != -1:
            body = body[:index].strip()

        # try period
        index = 0
        while index < max_length:
            next_index = body.find(u'. ', index)
            if next_index == -1:
                break
            index = next_index + 1

        if index > 3:
            return body[:index].strip()

        if len(body) <= max_length:
            return body

        # just cut-off
        return body[:max_length - 3].strip() + u'...'


class UserPreferences(ndb.Model):
    user = ndb.UserProperty()
    userpage_title = ndb.StringProperty()
    created_at = ndb.DateTimeProperty()

    @classmethod
    def save(cls, user, userpage_title):
        keyid = ndb.Key(cls, user.email()).string_id()
        preferences = cls.get_by_id(keyid)
        if preferences is None:
            preferences = cls(id=keyid)
            preferences.user = user
            preferences.created_at = datetime.now()

        preferences.userpage_title = userpage_title
        preferences.put()
        return preferences

    @classmethod
    def get_by_email(cls, email):
        keyid = ndb.Key(cls, email).string_id()
        return cls.get_by_id(keyid)


class WikiPage(ndb.Model, PageOperationMixin):
    re_normalize_title = re.compile(ur'([\[\]\(\)\~\!\@\#\$\%\^\&\*\-'
                                    ur'\=\+\\:\;\'\"\,\.\/\?\<\>\s]|'
                                    ur'\bthe\b|\ban?\b)')

    itemtype_path = ndb.StringProperty()
    title = ndb.StringProperty()
    body = ndb.TextProperty()
    description = ndb.StringProperty()
    revision = ndb.IntegerProperty()
    comment = ndb.StringProperty()
    modifier = ndb.UserProperty()
    acl_read = ndb.StringProperty()
    acl_write = ndb.StringProperty()
    inlinks = ndb.JsonProperty()
    outlinks = ndb.JsonProperty()
    related_links = ndb.JsonProperty()
    updated_at = ndb.DateTimeProperty()

    published_at = ndb.DateTimeProperty()
    published_to = ndb.StringProperty()
    older_title = ndb.StringProperty()
    newer_title = ndb.StringProperty()

    @property
    def is_old_revision(self):
        return False

    @property
    def rendered_body(self):
        value = cache.get_rendered_body(self.title)
        if value is None:
            value = super(WikiPage, self).rendered_body
            cache.set_rendered_body(self.title, value)
        return value

    def preview_rendered_body(self, body):
        """Preview rendered body without updating model"""
        self.body = body
        return super(WikiPage, self).rendered_body

    @property
    def data(self):
        value = cache.get_data(self.title)
        if value is None:
            value = super(WikiPage, self).data
            cache.set_data(self.title, value)
        return value

    @property
    def metadata(self):
        value = cache.get_metadata(self.title)
        if value is None:
            value = super(WikiPage, self).metadata
            cache.set_metadata(self.title, value)
        return value

    @property
    def hashbangs(self):
        value = cache.get_hashbangs(self.title)
        if value is None:
            value = super(WikiPage, self).hashbangs
            cache.set_hashbangs(self.title, value)
        return value

    def delete(self, user=None):
        if not is_admin_user(user):
            raise RuntimeError('Only admin can delete pages.')

        self.update_content('', self.revision, None, user, force_update=False, dont_create_rev=True)
        self.related_links = {}
        self.modifier = None
        self.updated_at = None
        self.revision = 0
        self.put()

        keys = [r.key for r in self.revisions]
        ndb.delete_multi(keys)

        cache.del_titles()

    def update_content(self, new_body, base_revision, comment='', user=None, force_update=False, dont_create_rev=False):
        if not force_update and self.body == new_body:
            return False

        # get old data amd metadata
        old_md = self.metadata
        old_data = self.data

        # validate contents
        ## validate schema data
        new_md = PageOperationMixin.parse_metadata(new_body)
        try:
            PageOperationMixin.parse_data(self.title, new_md['schema'], new_body)
        except Exception:
            raise ValueError('Invalid schema data')

        ## validate metadata
        if u'pub' in new_md and u'redirect' in new_md:
            raise ValueError('You cannot use "pub" and "redirect" metadata at '
                             'the same time.')
        if u'redirect' in new_md and len(PageOperationMixin.remove_metadata(new_body).strip()) != 0:
            raise ValueError('Page with "redirect" metadata cannot have a body '
                             'content.')
        if u'read' in new_md and new_md['content-type'] != 'text/x-markdown':
            raise ValueError('You cannot restrict read access of custom content-typed page.')

        ## validate revision
        if self.revision < base_revision:
            raise ValueError('Invalid revision number: %d' % base_revision)

        ## validate ToC
        if not TocGenerator(md.convert(new_body)).validate():
            raise ValueError("Duplicate paths not allowed")

        if self.revision != base_revision:
            # perform 3-way merge if needed
            base = WikiPageRevision.query(WikiPageRevision.title == self.title, WikiPageRevision.revision == base_revision).get().body
            merged = ''.join(Merge3(base, self.body, new_body).merge_lines())
            conflicted = len(re.findall(PageOperationMixin.re_conflicted, merged)) > 0
            if conflicted:
                raise ConflictError('Conflicted', base, new_body, merged)
            else:
                new_body = merged

        # delete rendered body, metadata, data cache
        cache.del_rendered_body(self.title)
        cache.del_hashbangs(self.title)
        cache.del_metadata(self.title)
        cache.del_data(self.title)

        # update model fields
        self.body = new_body
        self.modifier = user
        self.description = self.make_description(200)
        self.acl_read = new_md.get('read', '')
        self.acl_write = new_md.get('write', '')
        self.comment = comment
        if not dont_create_rev:
            self.revision += 1

        if not force_update:
            self.updated_at = datetime.now()

        # publish
        pub_old = u'pub' in old_md
        pub_new = u'pub' in new_md
        pub_old_title = None
        pub_new_title = None
        if pub_old:
            pub_old_title = old_md['pub']
        if pub_new:
            pub_new_title = new_md['pub']

        if pub_old and pub_new and (pub_old_title != pub_new_title):
            # if target page is changed
            self._unpublish(save=False)
            self._publish(title=pub_new_title, save=False)
        else:
            if pub_new:
                self._publish(title=pub_new_title, save=False)
            else:
                self._unpublish(save=False)

        # update itemtype_path
        self.itemtype_path = schema.get_itemtype_path(new_md['schema'])

        # save
        self.put()

        # create revision
        if not dont_create_rev:
            rev_key = self._rev_key()
            rev = WikiPageRevision(parent=rev_key, title=self.title, body=self.body,
                                   created_at=self.updated_at, revision=self.revision,
                                   comment=self.comment, modifier=self.modifier,
                                   acl_read=self.acl_read, acl_write=self.acl_write)
            rev.put()

        # deferred update schema data index
        new_data = self.data
        deferred.defer(self.rebuild_data_index_deferred, old_data, new_data)

        # update inlinks and outlinks
        old_redir = old_md.get('redirect')
        new_redir = new_md.get('redirect')
        self.update_links(old_redir, new_redir)

        # delete config and tittle cache
        if self.title == '.config':
            cache.del_config()
        if self.revision == 1:
            cache.del_titles()

        return True

    def rebuild_data_index(self):
        # delete all index for this page
        index = SchemaDataIndex.query(SchemaDataIndex.title == self.title).fetch()

        keys = [i.key for i in index]
        ndb.delete_multi(keys)

        # insert
        data = self.data
        for name, value in self._data_as_pairs(data):
            i = SchemaDataIndex(title=self.title, name=name, value=value, data=data)
            i.put()

    def _data_as_pairs(self, data):
        pairs = set([])
        for key, value in data.items():
            if type(value) == list:
                for v in value:
                    pairs.add((key, v))
            else:
                pairs.add((key, value))
        return pairs

    def rebuild_data_index_deferred(self, old_data, new_data):
        old_pairs = self._data_as_pairs(old_data)
        new_pairs = self._data_as_pairs(new_data)

        inserts = new_pairs.difference(old_pairs)
        deletes = old_pairs.difference(new_pairs)

        # insert
        indice = [SchemaDataIndex(title=self.title, name=name, value=value, data=new_data) for name, value in inserts]
        ndb.put_multi(indice)

        # delete
        keys = [SchemaDataIndex(title=self.title, name=name, value=value, data=new_data).key for name, value in deletes]
        keys = [key for key in keys if key is not None]
        ndb.delete_multi(keys)

    @property
    def revisions(self):
        return WikiPageRevision.query(ancestor=self._rev_key())

    @property
    def link_scoretable(self):
        """Returns all links ordered by score"""

        # related links
        related_links_scoretable = self.related_links

        # in/out links
        inlinks = reduce(lambda a, b: a + b, self.inlinks.values(), [])
        outlinks = reduce(lambda a, b: a + b, self.outlinks.values(), [])
        inout_links = set(inlinks + outlinks).difference(related_links_scoretable.keys())
        inout_links_len = len(inout_links)
        inout_score = 1.0 / inout_links_len if inout_links_len != 0 else 0.0
        inout_links_scoretable = dict(zip(inout_links, [inout_score] * inout_links_len))

        scoretable = dict(inout_links_scoretable.items() + related_links_scoretable.items())
        sorted_scoretable = sorted(scoretable.iteritems(),
                                   key=operator.itemgetter(1),
                                   reverse=True)
        return OrderedDict(sorted_scoretable)

    def update_links(self, old_redir, new_redir):
        """Updates outlinks of this page and inlinks of target pages"""
        # 1. process "redirect" metadata
        if old_redir != new_redir:
            if old_redir is not None:
                source = WikiPage.get_by_title(old_redir, follow_redirect=True)
            else:
                source = self

            if new_redir is not None:
                target = WikiPage.get_by_title(new_redir, follow_redirect=True)
            else:
                target = self

            for rel, titles in source.inlinks.items():
                for t in titles:
                    page = WikiPage.get_by_title(t)
                    page.del_outlink(source.title, rel)
                    page.add_outlink(target.title, rel)
                    page.put()
                    cache.del_rendered_body(page.title)
                    cache.del_hashbangs(page.title)

                target.add_inlinks(source.inlinks[rel], rel)
                del source.inlinks[rel]

            source.put()
            cache.del_rendered_body(source.title)
            cache.del_hashbangs(source.title)
            target.put()
            cache.del_rendered_body(target.title)
            cache.del_hashbangs(target.title)

        # 2. update in/out links
        cur_outlinks = self.outlinks or {}
        new_outlinks = {}
        for rel, titles in self._parse_outlinks().items():
            new_outlinks[rel] =\
                [WikiPage.get_by_title(t, follow_redirect=True).title
                 for t in titles]
            new_outlinks[rel] = list(set(new_outlinks[rel]))

        if self.acl_read:
            # delete all inlinks of target pages if there's read restriction
            for rel, titles in cur_outlinks.items():
                for title in titles:
                    page = WikiPage.get_by_title(title)
                    try:
                        page.del_inlink(title)
                        if len(page.inlinks) == 0 and page.revision == 0:
                            page.put().delete()
                        else:
                            page.put()
                        cache.del_rendered_body(page.title)
                        cache.del_hashbangs(page.title)
                    except ValueError:
                        pass
        else:
            # update all inlinks of target pages
            added_outlinks = {}
            for rel, titles in new_outlinks.items():
                added_outlinks[rel] = titles
                if rel in cur_outlinks:
                    added_outlinks[rel] =\
                        set(added_outlinks[rel]).difference(cur_outlinks[rel])
            removed_outlinks = {}
            for rel, titles in cur_outlinks.items():
                removed_outlinks[rel] = titles
                if rel in new_outlinks:
                    removed_outlinks[rel] =\
                        set(removed_outlinks[rel]).difference(new_outlinks[rel])

            for rel, titles in added_outlinks.items():
                for title in titles:
                    page = WikiPage.get_by_title(title)
                    page.add_inlink(self.title, rel)
                    page.put()
                    cache.del_rendered_body(page.title)
                    cache.del_hashbangs(page.title)
            for rel, titles in removed_outlinks.items():
                for title in titles:
                    page = WikiPage.get_by_title(title, follow_redirect=True)
                    try:
                        page.del_inlink(self.title, rel)
                        if page.inlinks == {} and page.revision == 0:
                            page.put().delete()
                        else:
                            page.put()
                        cache.del_rendered_body(page.title)
                        cache.del_hashbangs(page.title)
                    except ValueError:
                        pass

        # update outlinks of this page
        self.outlinks = new_outlinks
        for rel in self.outlinks.keys():
            self.outlinks[rel].sort()
        self.put()

    def _publish(self, title, save):
        if self.published_at is not None and self.published_to == title:
            return

        posts = WikiPage.get_published_posts(title, 20)

        if len(posts) > 0:
            latest = posts[0]
            latest.newer_title = self.title
            latest.put()
            self.older_title = latest.title

        self.published_to = title
        self.published_at = datetime.now()

        if save:
            self.put()

        cache.del_rendered_body(self.title)
        cache.del_hashbangs(self.title)
        if self.newer_title:
            cache.del_rendered_body(self.newer_title)
            cache.del_hashbangs(self.newer_title)
        if self.older_title:
            cache.del_rendered_body(self.older_title)
            cache.del_hashbangs(self.older_title)

    def _unpublish(self, save):
        if self.published_at is None:
            return

        cache.del_rendered_body(self.title)
        cache.del_hashbangs(self.title)
        if self.newer_title:
            cache.del_rendered_body(self.newer_title)
            cache.del_hashbangs(self.newer_title)
        if self.older_title:
            cache.del_rendered_body(self.older_title)
            cache.del_hashbangs(self.older_title)

        older = WikiPage.get_by_title(self.older_title)
        newer = WikiPage.get_by_title(self.newer_title)

        if self.older_title is not None and self.newer_title is not None:
            newer.older_title = self.older_title
            older.newer_title = self.newer_title
            newer.put()
            older.put()
        elif self.older_title is not None:
            older.newer_title = None
            older.put()
        elif self.newer_title is not None:
            newer.older_title = None
            newer.put()

        self.published_at = None
        self.published_to = None
        self.older_title = None
        self.newer_title = None

        if save:
            self.put()

    def get_similar_titles(self, user):
        return WikiPage._similar_titles(WikiPage.get_titles(user), self.title)

    def update_related_links(self, max_distance=5):
        """Update related_links score table by random walk"""
        if len(self.outlinks) == 0:
            return

        if self.related_links is None:
            self.related_links = {}

        # random walk
        score_table = self.related_links
        WikiPage._update_related_links(self, self, 0.1, score_table,
                                       max_distance)

        self.related_links = score_table
        self.normalize_related_links()

    def normalize_related_links(self):
        related_links = self.related_links

        # filter out obvious(direct) links
        outlinks = reduce(lambda x, y: x + y, self.outlinks.values(), [])
        inlinks = reduce(lambda x, y: x + y, self.inlinks.values(), [])
        direct_links = set(outlinks + inlinks)
        related_links = dict(filter(lambda (k, v): k not in direct_links, related_links.items()))

        # filter out insignificant links
        if len(related_links) > 30:
            sorted_tuples = sorted(related_links.iteritems(),
                                   key=operator.itemgetter(1))
            related_links = OrderedDict(sorted_tuples[-30:])

        # normalize score
        total = sum(related_links.values())
        if total > 1.0:
            for link, score in related_links.items():
                related_links[link] = score / total

        # done
        self.related_links = related_links

    def _parse_outlinks(self):
        unique_links = {}
        itemtype = self.itemtype

        # Add links in body
        links = md_wikilink.parse_wikilinks(itemtype, WikiPage.remove_metadata(self.body))
        for rel, titles in links.items():
            unique_links[rel] = set(titles)

        # Add links in structured data
        for name, value in self.data.items():
            if not self._is_schema_item_link(name):
                continue

            links = md_wikilink.parse_wikilinks(itemtype, u'[[%s::%s]]' % (name, value))
            for rel, titles in links.items():
                if rel not in unique_links:
                    unique_links[rel] = set([])
                unique_links[rel].update(titles)

        # turn sets into lists
        for key in unique_links.keys():
            unique_links[key] = list(unique_links[key])

        return unique_links

    def add_inlinks(self, titles, rel):
        WikiPage._add_inout_links(self.inlinks, titles, rel)

    def add_outlinks(self, titles, rel):
        WikiPage._add_inout_links(self.outlinks, titles, rel)

    def add_inlink(self, title, rel):
        WikiPage._add_inout_link(self.inlinks, title, rel)

    def add_outlink(self, title, rel):
        WikiPage._add_inout_link(self.outlinks, title, rel)

    def del_inlink(self, title, rel=None):
        WikiPage._del_inout_link(self.inlinks, title, rel)

    def del_outlink(self, title, rel=None):
        WikiPage._del_inout_link(self.outlinks, title, rel)

    @classmethod
    def search(cls, expression):
        # parse
        parsed = search.parse_expression(expression)

        # evaluate
        pos, neg = parsed['pos'], parsed['neg']
        pos_pages = [cls.get_by_title(t, True) for t in pos]
        neg_pages = [cls.get_by_title(t, True) for t in neg]
        scoretable = search.evaluate(
            dict((page.title, page.link_scoretable) for page in pos_pages),
            dict((page.title, page.link_scoretable) for page in neg_pages)
        )

        return scoretable

    @classmethod
    def randomly_update_related_links(cls, iteration, recent=False):
        if recent:
            titles = [p.title for p in WikiPage.get_changes(None)][:iteration]
        else:
            titles = WikiPage.get_titles()

        if len(titles) > iteration:
            titles = random.sample(titles, iteration)
        for title in titles:
            page = cls.get_by_title(title, follow_redirect=True)
            page.update_related_links()
            page.put()
        return titles

    @classmethod
    def _update_related_links(cls, start_page, page, score, score_table,
                              distance):
        if distance == 0:
            return

        #if l != start_page.title
        nested_links = [l for l in page.outlinks.values()]
        links = []
        for l in nested_links:
            links += l
        links = [l for l in links if l != start_page.title]

        if len(links) == 0:
            return

        next_page = WikiPage.get_by_title(random.choice(links), follow_redirect=True)
        next_link = next_page.title
        if next_link not in score_table:
            score_table[next_link] = 0.0

        next_score = score * 0.5
        score_table[next_link] = next_score

        # update target page's relate links
        if next_page.revision > 0:
            if next_page.related_links is None:
                next_page.related_links = {}
            if start_page.title not in next_page.related_links:
                next_page.related_links[start_page.title] = 0.0

            next_page_score = next_score
            next_page.related_links[start_page.title] += next_page_score
            next_page.normalize_related_links()
            next_page.put()

        cls._update_related_links(start_page, next_page, next_score,
                                  score_table, distance - 1)

    @classmethod
    def get_index(cls, user=None):
        q = WikiPage.query(ancestor=WikiPage._key())

        pages = q.order(WikiPage.title).fetch(projection=[
            WikiPage.title,
            WikiPage.acl_write,
            WikiPage.acl_read,
            WikiPage.comment,
            WikiPage.modifier,
            WikiPage.updated_at])

        default_permission = PageOperationMixin.get_default_permission()
        return [page for page in pages
                if page.updated_at and page.can_read(user, default_permission)]

    @classmethod
    def get_titles(cls, user=None):
        email = user.email() if user is not None else u'None'
        titles = cache.get_titles(email)
        if titles is None:
            titles = set([page.title for page in cls.get_index(user)])
            cache.set_titles(email, titles)

        return titles

    @staticmethod
    def get_published_posts(title, limit):
        q = WikiPage.query(ancestor=WikiPage._key())
        q = q.filter(WikiPage.published_to == title)
        q = q.filter(WikiPage.published_at != None)
        return list(q.order(-WikiPage.published_at).fetch(limit=limit))

    @classmethod
    def get_changes(cls, user, limit=7, include_body=False):
        q = WikiPage.query(ancestor=WikiPage._key())
        q = q.filter(WikiPage.updated_at != None)

        if limit != 0:
            date_from = datetime.now() - timedelta(days=limit)
            q = q.filter(WikiPage.updated_at >= date_from)

        if include_body:
            pages = q.order(-WikiPage.updated_at).fetch()
        else:
            prjs = [
                WikiPage.title,
                WikiPage.updated_at,
                WikiPage.modifier,
                WikiPage.comment,
                WikiPage.acl_write,
                WikiPage.acl_read,
            ]
            pages = q.order(-WikiPage.updated_at).fetch(projection=prjs)

        default_permission = PageOperationMixin.get_default_permission()
        return [page for page in pages if page.can_read(user, default_permission)]

    @classmethod
    def get_config(cls):
        result = cache.get_config()
        if result is None:
            result = main.DEFAULT_CONFIG

            try:
                page = cls.get_by_title('.config')
                user_config = yaml.load(PageOperationMixin.remove_metadata(page.body))
            except:
                user_config = None
            user_config = user_config or {}

            def merge_dict(target_dict, source_dict):
                for (key,value) in source_dict.iteritems():
                    if type(value) != dict:
                        target_dict[key] = value
                    else:
                        merge_dict(target_dict.setdefault(key, {}), value)

            merge_dict(result, user_config)

            cache.set_config(result)
        return result

    @classmethod
    def wikiquery(cls, q, user=None):
        email = user.email() if user is not None else 'None'
        results = cache.get_wikiquery(q, email)
        if results is None:
            page_query, attrs = search.parse_wikiquery(q)
            datas = cls._evaluate_pages(page_query)
            accessible_titles = WikiPage.get_titles(user)

            results = []
            for title, data in datas.items():
                if title in accessible_titles:
                    results.append(OrderedDict((attr, data[attr] if attr in data else None) for attr in attrs))

            if len(results) == 1:
                results = results[0]

            cache.set_wikiquery(q, email, results)
        return results

    @classmethod
    def _evaluate_pages(cls, q):
        if len(q) == 1:
            pages = cls._evaluate_pages(q[0])
        elif len(q) == 2:
            pages = cls._evaluate_page_query_term(q[0], q[1])
        else:
            pages = cls._evaluate_page_query_expr(q[0], q[1], q[2:])
        return pages

    @classmethod
    def _evaluate_page_query_term(cls, name, value):
        if name == 'schema' and value.find('/') == -1:
            value = schema.get_itemtype_path(value)

        pages = {}

        for index in SchemaDataIndex.query(SchemaDataIndex.name == name, SchemaDataIndex.value == value):
            pages[index.title] = index.data

        return pages

    @classmethod
    def _evaluate_page_query_expr(cls, operand, op, rest):
        pages1 = cls._evaluate_pages(operand)
        pages2 = cls._evaluate_pages(rest)
        pages = {}

        if op == '*':
            keys = set(pages1.keys()).intersection(set(pages2.keys()))
            for key in keys:
                if key in pages1:
                    pages[key] = pages1[key]
                else:
                    pages[key] = pages2[key]
        elif op == '+':
            keys = set(pages1.keys()).union(set(pages2.keys()))
            pages = dict((k, v) for k, v in (pages1.items() + pages2.items()) if k in keys)

        return pages

    @classmethod
    def get_by_title(cls, title, follow_redirect=False):
        if title is None:
            return None
        if title[0] == u'=':
            raise ValueError(u'WikiPage title cannot starts with "="')

        key = cls._key()
        page = WikiPage.query(WikiPage.title == title, ancestor=key).get()
        if page is None:
            page = WikiPage(parent=key, title=title, body=u'', revision=0,
                            inlinks={}, outlinks={}, related_links={})
        elif follow_redirect and 'redirect' in page.metadata:
            new_title = page.metadata['redirect']
            page = cls.get_by_title(new_title, follow_redirect)

        return page

    @classmethod
    def title_to_path(cls, title):
        return urllib2.quote(title.replace(u' ', u'_').encode('utf-8'))

    @classmethod
    def path_to_title(cls, path):
        return urllib2.unquote(path).decode('utf-8').replace('_', ' ')

    @classmethod
    def _similar_titles(cls, titles, target):
        normalized_target = cls._normalize_title(target)
        if len(normalized_target) == 0:
            return OrderedDict([
                (u'startswiths', []),
                (u'endswiths', []),
                (u'contains', []),
            ])

        contains = []
        startswiths = []
        endswiths = []
        for title in titles:
            normalized_title = cls._normalize_title(title)
            if normalized_title.find(normalized_target) == -1:
                continue
            if normalized_title.startswith(normalized_target):
                startswiths.append(title)
            elif normalized_title.endswith(normalized_target):
                endswiths.append(title)
            else:
                contains.append(title)

        return OrderedDict([
            (u'startswiths', startswiths),
            (u'endswiths', endswiths),
            (u'contains', contains),
        ])

    @classmethod
    def _normalize_title(cls, title):
        return re.sub(cls.re_normalize_title, u'', title.lower())

    @classmethod
    def _key(cls):
        return ndb.Key(u'wiki', u'/')

    @staticmethod
    def _add_inout_links(links, titles, rel):
        if len(titles) == 0:
            return

        if rel not in links:
            links[rel] = []

        links[rel] += titles
        links[rel].sort()

    @staticmethod
    def _add_inout_link(links, title, rel):
        if rel not in links:
            links[rel] = []
        if title not in links[rel]:
            links[rel].append(title)
            links[rel].sort()

    @staticmethod
    def _del_inout_link(links, title, rel=None):
        if rel is not None and rel in links:
            links[rel].remove(title)
            if len(links[rel]) == 0:
                del links[rel]
        else:
            for rel, titles in links.items():
                titles.remove(title)
                if len(titles) == 0:
                    del links[rel]

    @classmethod
    def rebuild_all_data_index(cls, page_index=0):
        logging.debug('Rebuilding data index: %d' % page_index)

        batch_size = 20
        all_pages = list(cls.query().fetch(batch_size, offset=page_index * batch_size))
        if len(all_pages) == 0:
            logging.debug('Rebuilding data index: Finished!')
            return

        for p in all_pages:
            p.rebuild_data_index()

        deferred.defer(cls.rebuild_all_data_index, page_index + 1)

    def _rev_key(self):
        return ndb.Key(u'revision', self.title)


class WikiPageRevision(ndb.Model, PageOperationMixin):
    title = ndb.StringProperty()
    body = ndb.TextProperty()
    revision = ndb.IntegerProperty()
    comment = ndb.StringProperty()
    modifier = ndb.UserProperty()
    acl_read = ndb.StringProperty()
    acl_write = ndb.StringProperty()
    created_at = ndb.DateTimeProperty()

    @property
    def absolute_url(self):
        return u'/%s?rev=%d' % (WikiPage.title_to_path(self.title), self.revision)

    @property
    def is_old_revision(self):
        return True

    @property
    def updated_at(self):
        return self.created_at

    @property
    def inlinks(self):
        return {}

    @property
    def outlinks(self):
        return {}

    @property
    def related_links(self):
        return {}

    @property
    def older_title(self):
        return None

    @property
    def newer_title(self):
        return None


class SchemaDataIndex(ndb.Model):
    title = ndb.StringProperty()
    name = ndb.StringProperty()
    value = ndb.StringProperty()
    data = ndb.JsonProperty()


class TocGenerator(object):
    re_headings = ur'<h(\d)>(.+?)</h\d>'

    def __init__(self, html):
        self._html = html
        self._index = 0

    def validate(self):
        try:
            headings = TocGenerator.extract_headings(self._html)
            outlines = self._generate_outline(headings)
            self._generate_path(outlines)
            return True
        except ValueError:
            return False

    def add_toc(self):
        """Add table of contents to HTML"""
        headings = TocGenerator.extract_headings(self._html)
        outlines = self._generate_outline(headings)
        paths = self._generate_path(outlines)

        if len(headings) > 4:
            toc = u'<div class="toc"><h1>Table of Contents</h1>' \
                  u'%s</div>' % self._generate_toc(outlines, iter(paths))
        else:
            toc = u''

        def replacer(m):
            lev = m.group(1)
            text = m.group(2)
            hashed = TocGenerator.hash_str(paths[self._index])
            html = u'<h%s>%s ' \
                   u'<a id="h_%s" href="#h_%s" class="caret-target">#</a>' \
                   u'</h%s>' % (lev, text, hashed, hashed, lev)

            # insert table of contents before first heading
            if self._index == 0:
                html = toc + html

            self._index += 1
            return html

        return re.sub(TocGenerator.re_headings, replacer, self._html)

    @staticmethod
    def extract_headings(html):
        matches = re.findall(TocGenerator.re_headings, html, flags=re.DOTALL)
        return [(int(m[0]), m[1]) for m in matches]

    @staticmethod
    def hash_str(path):
        m = hashlib.md5()
        m.update(path.encode('utf-8'))
        return m.hexdigest()

    def _generate_outline(self, headings):
        """Generate recursive array of document headings"""
        if len(headings) == 0:
            return []

        cur_lev = headings[0][0]
        if cur_lev != 1:
            raise ValueError('Headings should start from H1')

        _, result = self._outline_children(headings, 0, cur_lev)
        return result

    def _generate_toc(self, outline, path_iter):
        if len(outline) == 0:
            return u''

        parts = [u'<ol>']
        for title, children in outline:
            hashed = TocGenerator.hash_str(path_iter.next())
            title = re.sub(ur'<[^>]+>', '', title)
            parts.append(u'<li>')
            parts.append(u'<div><a href="#h_%s">%s</a></div>' % (hashed, title))
            parts.append(self._generate_toc(children, path_iter))
            parts.append(u'</li>')
        parts.append(u'</ol>')

        return u''.join(parts)

    def _outline_children(self, hs, index, lev):
        result = []
        while len(hs) > index:
            curlev, curtitle = hs[index]
            if curlev == lev:
                index, children = self._outline_children(hs, index + 1, curlev + 1)
                result.append([curtitle, children])
                index += 1
            elif curlev < lev:
                index -= 1
                break
            else:
                raise ValueError('Invalid level of headings')

        return index, result

    def _generate_path(self, outlines):
        result = []
        self._generate_children_path(result, None, outlines)

        duplicates = set([x for x in result if result.count(x) > 1])
        if len(duplicates) > 0:
            raise ValueError("Duplicate paths not allowed: %s" % duplicates.pop())

        return result

    def _generate_children_path(self, result, path, outlines):
        for h, children in outlines:
            if path is not None:
                cur_path = u'%s\t%s' % (path, h)
            else:
                cur_path = h
            result.append(cur_path)
            self._generate_children_path(result, cur_path, children)


class ConflictError(ValueError):
    def __init__(self, message, base, provided, merged):
        Exception.__init__(self, message)
        self.base = base
        self.provided = provided
        self.merged = merged


regions = {
    u'ㄱ': (u'가', u'나'),
    u'ㄴ': (u'나', u'다'),
    u'ㄷ': (u'다', u'라'),
    u'ㄹ': (u'라', u'마'),
    u'ㅁ': (u'마', u'바'),
    u'ㅂ': (u'바', u'사'),
    u'ㅅ': (u'사', u'아'),
    u'ㅇ': (u'아', u'자'),
    u'ㅈ': (u'자', u'차'),
    u'ㅊ': (u'차', u'카'),
    u'ㅋ': (u'카', u'타'),
    u'ㅌ': (u'타', u'파'),
    u'ㅍ': (u'파', u'하'),
    u'ㅎ': (u'하', u'힣'),
}


def title_grouper(title):
    title = title.upper()
    head = title[0]
    if 'A' <= head <= 'Z' or '0' <= head <= '9':
        return head

    for key, values in regions.items():
        if values[0] <= head < values[1]:
            return key

    return 'Misc'


def is_admin_user(user):
    if not user:
        return False

    if users.is_current_user_admin():
        return True

    try:
        if oauth.is_current_user_admin():
            return True
    except oauth.OAuthRequestError:
        pass

    return False


md = markdown.Markdown(
    extensions=[
        md_wikilink.WikiLinkExtension(),
        md_itemprop.ItemPropExtension(),
        md_url.URLExtension(),
        md_mathjax.MathJaxExtension(),
        md_strikethrough.StrikethroughExtension(),
        DefListExtension(),
        AttrListExtension(),
    ],
    safe_mode=False,
)
