# -*- coding: utf-8 -*-
# Amara, universalsubtitles.org
#
# Copyright (C) 2012 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

import json
import os
from datetime import datetime

import math_captcha
import babelsubs
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.db.models import ObjectDoesNotExist
from django.test import TestCase
from vidscraper.sites import blip

from apps.auth.models import CustomUser as User
from apps.subtitles.pipeline import add_subtitles
from apps.testhelpers.views import _create_videos
from apps.videos import metadata_manager
from apps.videos.forms import VideoForm
from apps.videos.models import (
    Video, Action, VIDEO_TYPE_YOUTUBE, UserTestResult, SubtitleLanguage,
    VideoUrl, Subtitle, SubtitleVersion, VIDEO_TYPE_HTML5
)
from apps.videos.rpc import VideosApiClass
from apps.videos.share_utils import _make_email_url
from apps.videos.tasks import video_changed_tasks
from apps.videos.types import video_type_registrar
from apps.widget import video_cache
from apps.widget.rpc import Rpc
from apps.widget.tests import create_two_sub_session, RequestMockup

from utils.unisubsmarkup import html_to_markup, markup_to_html


math_captcha.forms.math_clean = lambda form: None

SRT = u"""1
00:00:00,004 --> 00:00:02,093
We\n started <b>Universal Subtitles</b> <i>because</i> we <u>believe</u>
"""

def create_langs_and_versions(video, langs, user=None):
    from subtitles import pipeline

    subtitles = (babelsubs.load_from(SRT, type='srt', language='en')
                          .to_internal())
    return [pipeline.add_subtitles(video, l, subtitles) for l in langs]

class GenericTest(TestCase):
    def test_languages(self):
        langs = [l[1] for l in settings.ALL_LANGUAGES]
        langs_set = set(langs)
        self.assertEqual(len(langs), len(langs_set))

class BusinessLogicTest(TestCase):
    fixtures = ['staging_users.json', 'staging_videos.json']

    def setUp(self):
        self.auth = dict(username='admin', password='admin')
        self.user = User.objects.get(username=self.auth['username'])

    def test_rollback_to_dependent(self):
        """
        Here is the use case:
        we have en -> fr, both on version 0
        fr is forked and edited, fr is now on version 1
        now, we rollback to fr.
        we should have french on v2 as a dependent language of en
        addign a sub to en should make french have that sub as well
        """
        data = {
            "url": "http://www.example.com/sdf.mp4",
            "langs": [
                {
                    "code": "en",
                    "num_subs": 4,
                    "is_complete": False,
                    "is_original": True,
                    "translations": [
                        {
                            "code": "fr",
                            "num_subs": 4,
                            "is_complete": True,
                            "is_original": False,
                            "translations": [],
                        }],
                }],

            "title": "c" }

        _create_videos([data], [])
        v = Video.objects.get(title='c')

        en = v.subtitle_language('en')
        en_version = en.version()
        fr = v.subtitle_language('fr')
        fr_version = fr.version()
        for ens, frs in zip(en_version.ordered_subtitles(),
                            fr_version.ordered_subtitles()):
            self.assertEqual(ens.start_time, frs.start_time)
            self.assertEqual(ens.end_time, frs.end_time)
        self.assertFalse(fr.is_forked)
        # now, for on uploade
        self.client.login(**self.auth)

        rpc = Rpc()
        request = RequestMockup(user=self.user)
        request.user = self.user
        return_value = rpc.start_editing(
            request,
            v.video_id,
            "fr",
            base_language_pk=en.pk
        )
        session_pk = return_value['session_pk']
        inserted = [{'subtitle_id': 'aa',
                     'text': 'hey!',
                     'start_time': 2.3,
                     'end_time': 3.4,
                     'sub_order': 4.0}]
        rpc.finished_subtitles(request, session_pk, inserted, forked=True);

        fr = refresh_obj(fr)
        self.assertEquals(fr.subtitleversion_set.all().count(), 2)
        self.assertTrue(fr.is_forked)
        self.assertFalse(Subtitle.objects.filter(version=fr_version).count() ==
                         Subtitle.objects.filter(version=fr.version()).count() )
        # now, when we rollback, we want to make sure we end up with
        # the correct subs and a non forked language
        fr_version = refresh_obj(fr_version)
        fr_version.rollback(self.user)
        fr = refresh_obj(fr)
        fr_version_2 = fr.version()
        self.assertFalse(fr_version_2== fr_version)
        new_subs = fr_version_2.ordered_subtitles()
        old_subs = fr_version.ordered_subtitles()
        self.assertEqual(len(new_subs), len(old_subs))
        for ens, frs in zip(en_version.ordered_subtitles(),
                            fr_version_2.ordered_subtitles()):
            self.assertEqual(ens.start_time, frs.start_time)
            self.assertEqual(ens.end_time, frs.end_time)
        self.assertFalse(fr.is_forked)

    def test_first_approved(self):
        from apps.teams.moderation_const import APPROVED
        language = SubtitleLanguage.objects.all()[0]

        for i in range(1, 10):
            SubtitleVersion.objects.create(language=language,
                    datetime_started=datetime(2012, 1, i, 0, 0, 0),
                    version_no=i)

        v1 = SubtitleVersion.objects.get(language=language, version_no=3)
        v2 = SubtitleVersion.objects.get(language=language, version_no=6)

        v1.moderation_status = APPROVED
        v1.save()
        v2.moderation_status = APPROVED
        v2.save()

        self.assertEquals(v1.pk, language.first_approved_version.pk)

class WebUseTest(TestCase):
    def _make_objects(self, video_id="S7HMxzLmS9gw"):
        self.auth = dict(username='admin', password='admin')
        self.user = User.objects.get(username=self.auth['username'])
        self.video = Video.objects.get(video_id=video_id)
        self.video.followers.add(self.user)

    def _simple_test(self, url_name, args=None, kwargs=None, status=200, data={}):
        response = self.client.get(reverse(url_name, args=args, kwargs=kwargs), data)
        self.assertEqual(response.status_code, status)
        return response

    def _login(self):
        self.client.login(**self.auth)


class Html5ParseTest(TestCase):
    def _assert(self, start_url, end_url):
        video, created = Video.get_or_create_for_url(start_url)
        vu = video.videourl_set.all()[:1].get()
        self.assertEquals(VIDEO_TYPE_HTML5, vu.type)
        self.assertEquals(end_url, vu.url)

    def test_ogg(self):
        self._assert(
            'http://videos.mozilla.org/firefox/3.5/switch/switch.ogv',
            'http://videos.mozilla.org/firefox/3.5/switch/switch.ogv')

    def test_blip_ogg(self):
        self._assert(
            'http://a59.video2.blip.tv/8410006747301/Miropcf-AboutUniversalSubtitles847.ogv',
            'http://a59.video2.blip.tv/8410006747301/Miropcf-AboutUniversalSubtitles847.ogv')

    def test_blip_ogg_with_query_string(self):
        self._assert(
            'http://a59.video2.blip.tv/8410006747301/Miropcf-AboutUniversalSubtitles847.ogv?bri=1.4&brs=1317',
            'http://a59.video2.blip.tv/8410006747301/Miropcf-AboutUniversalSubtitles847.ogv')

    def test_mp4(self):
        self._assert(
            'http://videos.mozilla.org/firefox/3.5/switch/switch.mp4',
            'http://videos.mozilla.org/firefox/3.5/switch/switch.mp4')

    def test_blip_mp4_with_file_get(self):
        self._assert(
            'http://blip.tv/file/get/Miropcf-AboutUniversalSubtitles847.mp4',
            'http://blip.tv/file/get/Miropcf-AboutUniversalSubtitles847.mp4')

    def test_blip_mp4_with_query_string(self):
        self._assert(
            'http://a59.video2.blip.tv/8410006747301/Miropcf-AboutUniversalSubtitles847.mp4?bri=1.4&brs=1317',
            'http://a59.video2.blip.tv/8410006747301/Miropcf-AboutUniversalSubtitles847.mp4')

class VideoTest(TestCase):
    def setUp(self):
        self.user = User.objects.all()[0]
        self.youtube_video = 'http://www.youtube.com/watch?v=pQ9qX8lcaBQ'
        self.html5_video = 'http://mirrorblender.top-ix.org/peach/bigbuckbunny_movies/big_buck_bunny_1080p_stereo.ogg'

    def test_video_create(self):
        self._create_video(self.youtube_video)
        self._create_video(self.html5_video)

    def _create_video(self, video_url):
        video, created = Video.get_or_create_for_url(video_url)
        self.failUnless(video)
        self.failUnless(created)
        more_video, created = Video.get_or_create_for_url(video_url)
        self.failIf(created)
        self.failUnlessEqual(video, more_video)

    def test_video_cache_busted_on_delete(self):
        start_url = 'http://videos.mozilla.org/firefox/3.5/switch/switch.ogv'
        video, created = Video.get_or_create_for_url(start_url)
        video_url = video.get_video_url()
        video_pk = video.pk

        cache_id_1 = video_cache.get_video_id(video_url)
        self.assertTrue(cache_id_1)
        video.delete()
        self.assertEqual(Video.objects.filter(pk=video_pk).count() , 0)
        # when cache is not cleared this will return arn
        cache_id_2 = video_cache.get_video_id(video_url)
        self.assertNotEqual(cache_id_1, cache_id_2)
        # create a new video with the same url, has to have same# key
        video2, created= Video.get_or_create_for_url(start_url)
        video_url = video2.get_video_url()
        cache_id_3 = video_cache.get_video_id(video_url)
        self.assertEqual(cache_id_3, cache_id_2)

class RpcTest(TestCase):
    fixtures = ['test.json']

    def setUp(self):
        self.rpc = VideosApiClass()
        self.user = User.objects.get(username='admin')
        self.video = Video.objects.get(video_id='iGzkk7nwWX8F')


    def test_change_title_video(self):
        title = u'New title'
        self.rpc.change_title_video(self.video.pk, title, self.user)

        video = Video.objects.get(pk=self.video.pk)
        self.assertEqual(video.title, title)
        try:
            Action.objects.get(video=self.video, new_video_title=title,
                               action_type=Action.CHANGE_TITLE)
        except Action.DoesNotExist:
            self.fail()

class ViewsTest(WebUseTest):
    fixtures = ['test.json']

    def setUp(self):
        self._make_objects("iGzkk7nwWX8F")
        cache.clear()

    def test_video_url_make_primary(self):
        self._login()
        v = Video.objects.get(video_id='iGzkk7nwWX8F')
        self.assertNotEqual(len(VideoUrl.objects.filter(video=v)), 0)
        # add another url
        secondary_url = 'http://www.youtube.com/watch?v=po0jY4WvCIc'
        data = {
            'url': secondary_url,
            'video': v.pk
        }
        url = reverse('videos:video_url_create')
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        vid_url = 'http://www.youtube.com/watch?v=rKnDgT73v8s'
        # test make primary
        vu = VideoUrl.objects.filter(video=v)
        vu[0].make_primary()
        self.assertEqual(VideoUrl.objects.get(video=v, primary=True).url, vid_url)
        # check for activity
        self.assertEqual(len(Action.objects.filter(video=v, action_type=Action.EDIT_URL)), 1)
        vu[1].make_primary()
        self.assertEqual(VideoUrl.objects.get(video=v, primary=True).url, secondary_url)
        # check for activity
        self.assertEqual(len(Action.objects.filter(video=v, action_type=Action.EDIT_URL)), 2)
        # assert correct VideoUrl is retrieved
        self.assertEqual(VideoUrl.objects.filter(video=v)[0].url, secondary_url)

    def test_video_url_make_primary_team_video(self):
        self._login()
        v = Video.objects.get(video_id='KKQS8EDG1P4')
        self.assertNotEqual(len(VideoUrl.objects.filter(video=v)), 0)
        # add another url
        secondary_url = 'http://www.youtube.com/watch?v=tKTZoB2Vjuk'
        data = {
            'url': secondary_url,
            'video': v.pk
        }
        url = reverse('videos:video_url_create')
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        vid_url = 'http://www.youtube.com/watch?v=KKQS8EDG1P4'
        # test make primary
        vu = VideoUrl.objects.filter(video=v)
        vu[0].make_primary()
        self.assertEqual(VideoUrl.objects.get(video=v, primary=True).url, vid_url)
        # check for activity
        self.assertEqual(len(Action.objects.filter(video=v, action_type=Action.EDIT_URL)), 1)
        vu[1].make_primary()
        self.assertEqual(VideoUrl.objects.get(video=v, primary=True).url, secondary_url)
        # check for activity
        self.assertEqual(len(Action.objects.filter(video=v, action_type=Action.EDIT_URL)), 2)
        # assert correct VideoUrl is retrieved
        self.assertEqual(VideoUrl.objects.filter(video=v)[0].url, secondary_url)

    def test_index(self):
        self._simple_test('videos.views.index')

    def test_feedback(self):
        data = {
            'email': 'test@test.com',
            'message': 'Test',
            'math_captcha_field': 100500,
            'math_captcha_question': 'test'
        }
        response = self.client.post(reverse('videos:feedback'), data)
        self.assertEqual(response.status_code, 200)

    def test_create(self):
        self._login()
        url = reverse('videos:create')

        self._simple_test('videos:create')

        data = {
            'video_url': 'http://www.youtube.com/watch?v=osexbB_hX4g&feature=popular'
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        try:
            video = Video.objects.get(videourl__videoid='osexbB_hX4g',
                                      videourl__type=VIDEO_TYPE_YOUTUBE)
        except Video.DoesNotExist:
            self.fail()

        self.assertEqual(response['Location'], 'http://testserver' +
                                               video.get_absolute_url())

        len_before = Video.objects.count()
        data = {
            'video_url': 'http://www.youtube.com/watch?v=osexbB_hX4g'
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len_before, Video.objects.count())
        self.assertEqual(response['Location'], 'http://testserver' +
                                               video.get_absolute_url())

    def test_video_url_create(self):
        self._login()
        v = Video.objects.all()[:1].get()

        user = User.objects.exclude(id=self.user.id)[:1].get()
        user.notify_by_email = True
        user.is_active = True
        user.save()
        v.followers.add(user)

        data = {
            'url': u'http://www.youtube.com/watch?v=po0jY4WvCIc&feature=grec_index',
            'video': v.pk
        }
        url = reverse('videos:video_url_create')
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        try:
            v.videourl_set.get(videoid='po0jY4WvCIc')
        except ObjectDoesNotExist:
            self.fail()
        self.assertEqual(len(mail.outbox), 1)

    def test_video_url_remove(self):
        self._login()
        v = Video.objects.get(video_id='iGzkk7nwWX8F')
        # add another url since primary can't be removed
        data = {
            'url': 'http://www.youtube.com/watch?v=po0jY4WvCIc',
            'video': v.pk
        }
        url = reverse('videos:video_url_create')
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        vid_urls = VideoUrl.objects.filter(video=v)
        self.assertEqual(len(vid_urls), 2)
        vurl_id = vid_urls[1].id
        # check cache
        self.assertEqual(len(video_cache.get_video_urls(v.video_id)), 2)
        response = self.client.get(reverse('videos:video_url_remove'), {'id': vurl_id})
        # make sure get is not allowed
        self.assertEqual(response.status_code, 405)
        # check post
        response = self.client.post(reverse('videos:video_url_remove'), {'id': vurl_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(VideoUrl.objects.filter(video=v)), 1)
        self.assertEqual(len(Action.objects.filter(video=v, \
            action_type=Action.DELETE_URL)), 1)
        # assert cache is invalidated
        self.assertEqual(len(video_cache.get_video_urls(v.video_id)), 1)

    def test_video_url_deny_remove_primary(self):
        self._login()
        v = Video.objects.get(video_id='iGzkk7nwWX8F')
        vurl_id = VideoUrl.objects.filter(video=v)[0].id
        # make primary
        vu = VideoUrl.objects.filter(video=v)
        vu[0].make_primary()
        response = self.client.post(reverse('videos:video_url_remove'), {'id': vurl_id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(VideoUrl.objects.filter(video=v)), 1)

    def test_video(self):
        self.video.title = 'title'
        self.video.save()
        response = self.client.get(self.video.get_absolute_url(), follow=True)
        self.assertEqual(response.status_code, 200)

        self.video.title = ''
        self.video.save()
        response = self.client.get(self.video.get_absolute_url(), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_access_video_page_no_original(self):
        request = RequestMockup(User.objects.all()[0])
        session = create_two_sub_session(request)
        video_pk = session.language.video.pk
        video = Video.objects.get(pk=video_pk)
        en = video.subtitlelanguage_set.all()[0]
        en.is_original=False
        en.save()
        video_changed_tasks.delay(video_pk)
        response = self.client.get(reverse('videos:history', args=[video.video_id]))
        # Redirect for now, until we remove the concept of SubtitleLanguages
        # with blank language codes.
        self.assertEqual(response.status_code, 302)

    def test_bliptv_twice(self):
        VIDEO_FILE = 'http://blip.tv/file/get/Kipkay-AirDusterOfficeWeaponry223.m4v'
        old_video_file_url = blip.video_file_url
        blip.video_file_url = lambda x: VIDEO_FILE
        Video.get_or_create_for_url('http://blip.tv/file/4395490')
        blip.video_file_url = old_video_file_url
        # this test passes if the following line executes without throwing an error.
        Video.get_or_create_for_url(VIDEO_FILE)

    def test_legacy_history(self):
        # TODO: write tests
        pass

    def test_stop_notification(self):
        # TODO: write tests
        pass

    def test_subscribe_to_updates(self):
        # TODO: write test
        pass

    def test_email_friend(self):
        self._simple_test('videos:email_friend')

        data = {
            'from_email': 'test@test.com',
            'to_emails': 'test1@test.com,test@test.com',
            'subject': 'test',
            'message': 'test',
            'math_captcha_field': 100500,
            'math_captcha_question': 'test'
        }
        response = self.client.post(reverse('videos:email_friend'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEquals(len(mail.outbox), 1)

        mail.outbox = []
        data['link'] = 'http://someurl.com'
        self._login()
        response = self.client.post(reverse('videos:email_friend'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEquals(len(mail.outbox), 1)

        msg = u'Hey-- just found a version of this video ("Tú - Jennifer Lopez") with captions: http://unisubs.example.com:8000/en/videos/OcuMvG3LrypJ/'
        url = _make_email_url(msg)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_demo(self):
        self._simple_test('videos:demo')

    def test_history(self):
        # Redirect for now, until we remove the concept of SubtitleLanguages
        # with blank language codes.
        self._simple_test('videos:history',
            [self.video.video_id], status=302)

        self._simple_test('videos:history',
            [self.video.video_id], data={'o': 'user', 'ot': 'asc'}, status=302)

        sl = self.video.subtitlelanguage_set.all()[:1].get()
        sl.language = 'en'
        sl.save()
        self._simple_test('videos:translation_history',
            [self.video.video_id, sl.language, sl.id])

    def _test_rollback(self):
        #TODO: Seems like roll back is not getting called (on models)
        self._login()

        version = self.video.version(0)
        last_version = self.video.version(public_only=False)

        self._simple_test('videos:rollback', [version.id], status=302)

        new_version = self.video.version()
        self.assertEqual(last_version.version_no+1, new_version.version_no)

    def test_model_rollback(self):
        video = Video.objects.all()[:1].get()
        lang = video.subtitlelanguage_set.all()[:1].get()
        v = lang.latest_version(public_only=True)
        v.is_forked = True
        v.save()

        new_v = SubtitleVersion(language=lang, version_no=v.version_no+1,
                                datetime_started=datetime.now())
        new_v.save()
        lang = SubtitleLanguage.objects.get(id=lang.id)

        self._login()

        self.client.get(reverse('videos:rollback', args=[v.id]), {})
        lang = SubtitleLanguage.objects.get(id=lang.id)
        last_v = lang.latest_version(public_only=True)
        self.assertTrue(last_v.is_forked)
        self.assertFalse(last_v.notification_sent)
        self.assertEqual(last_v.version_no, new_v.version_no+1)

    def test_rollback_updates_sub_count(self):
        video = Video.objects.all()[:1].get()
        lang = video.subtitlelanguage_set.all()[:1].get()
        v = lang.latest_version(public_only=False)
        num_subs = len(v.subtitles())
        v.is_forked  = True
        v.save()
        new_v = SubtitleVersion(language=lang, version_no=v.version_no+1,
                                datetime_started=datetime.now())
        new_v.save()
        for i in xrange(0,20):
            s, created = Subtitle.objects.get_or_create(
                version=new_v,
                subtitle_id= "%s" % i,
                subtitle_order=i,
                subtitle_text="%s lala" % i
            )
        self._login()
        self.client.get(reverse('videos:rollback', args=[v.id]), {})
        last_v  = (SubtitleLanguage.objects.get(id=lang.id)
                                           .latest_version(public_only=True))
        final_num_subs = len(last_v.subtitles())
        self.assertEqual(final_num_subs, num_subs)

    def test_diffing(self):
        version = self.video.version(version_number=1)
        last_version = self.video.version()
        response = self._simple_test('videos:diffing', [version.id, last_version.id])
        self.assertEqual(len(response.context['captions']), 5)

    def test_test_form_page(self):
        self._simple_test('videos:test_form_page')

        data = {
            'email': 'test@test.ua',
            'task1': 'test1',
            'task2': 'test2',
            'task3': 'test3'
        }
        response = self.client.post(reverse('videos:test_form_page'), data)
        self.assertEqual(response.status_code, 302)

        try:
            UserTestResult.objects.get(**data)
        except UserTestResult.DoesNotExist:
            self.fail()

    def test_search(self):
        self._simple_test('search:index')

    def test_counter(self):
        self._simple_test('counter')

    def test_test_mp4_page(self):
        self._simple_test('test-mp4-page')

    def test_test_ogg_page(self):
        self._simple_test('test-ogg-page')

    def test_opensubtitles2010_page(self):
        self._simple_test('opensubtitles2010_page')

    def test_faq_page(self):
        self._simple_test('faq_page')

    def test_about_page(self):
        self._simple_test('about_page')

    def test_demo_page(self):
        self._simple_test('demo')

    def test_policy_page(self):
        self._simple_test('policy_page')

    def test_volunteer_page_category(self):
        self._login()
        categories = ['featured', 'popular', 'requested', 'latest']
        for category in categories:
            url = reverse('videos:volunteer_category',
                          kwargs={'category': category})

            response = self.client.post(url)
            self.assertEqual(response.status_code, 200)


class TestModelsSaving(TestCase):

    fixtures = ['test.json', 'subtitle_fixtures.json']

    def setUp(self):
        self.video = Video.objects.all()[:1].get()
        self.language = self.video.subtitle_language()

    def test_video_languages_count(self):
        from videos.tasks import video_changed_tasks

        #test if fixtures has correct data
        langs_count = self.video.newsubtitlelanguage_set.having_nonempty_tip().count()

        self.assertEqual(self.video.languages_count, langs_count)
        self.assertTrue(self.video.languages_count > 0)

        self.video.languages_count = 0
        self.video.save()
        video_changed_tasks.delay(self.video.pk)
        self.video = Video.objects.get(id=self.video.id)
        self.assertEqual(self.video.languages_count, langs_count)

    def test_subtitle_language_save(self):
        self.assertEqual(self.video.complete_date, None)
        self.assertEqual(self.video.subtitlelanguage_set.count(), 1)

        self.language.subtitles_complete = True
        self.language.save()
        from videos.tasks import video_changed_tasks
        video_changed_tasks.delay(self.video.pk)
        self.video = Video.objects.get(pk=self.video.pk)
        self.assertNotEqual(self.video.complete_date, None)

        self.language.subtitles_complete = False
        self.language.save()
        video_changed_tasks.delay(self.video.pk)
        self.video = Video.objects.get(pk=self.video.pk)
        self.assertEqual(self.video.complete_date, None)

        version = create_langs_and_versions(self.video, ['ru'])
        new_language = version[0].subtitle_language
        new_language.subtitles_complete = True
        new_language.save()
        video_changed_tasks.delay(self.video.pk)
        self.video = Video.objects.get(pk=self.video.pk)
        self.assertNotEqual(self.video.complete_date, None)

        self.language.subtitles_complete = True
        self.language.save()
        video_changed_tasks.delay(self.video.pk)
        self.video = Video.objects.get(pk=self.video.pk)
        self.assertNotEqual(self.video.complete_date, None)

        new_language.subtitles_complete = False
        new_language.save()
        video_changed_tasks.delay(self.video.pk)
        self.video = Video.objects.get(pk=self.video.pk)
        self.assertNotEqual(self.video.complete_date, None)

        self.language.subtitles_complete = False
        self.language.save()
        video_changed_tasks.delay(self.video.pk)
        self.video = Video.objects.get(pk=self.video.pk)
        self.assertEqual(self.video.complete_date, None)

class TestVideoForm(TestCase):
    def setUp(self):
        self.vimeo_urls = ("http://vimeo.com/17853047",)
        self.youtube_urls = ("http://youtu.be/HaAVZ2yXDBo", "http://www.youtube.com/watch?v=HaAVZ2yXDBo")
        self.html5_urls = ("http://blip.tv/file/get/Miropcf-AboutUniversalSubtitles715.mp4",)
        self.daily_motion_urls = ("http://www.dailymotion.com/video/xb0hsu_qu-est-ce-que-l-apache-software-fou_tech",)

    def _test_urls(self, urls):
        for url in urls:
            form = VideoForm(data={"video_url":url})
            self.assertTrue(form.is_valid())
            video = form.save()
            video_type = video_type_registrar.video_type_for_url(url)
            # double check we never confuse video_id with video.id with videoid, sigh
            model_url = video.get_video_url()
            if hasattr(video_type, "videoid"):
                self.assertTrue(video_type.videoid  in model_url)
            # check the pk is never on any of the urls parts
            for part in model_url.split("/"):
                self.assertTrue(str(video.pk)  != part)
            self.assertTrue(video.video_id  not in model_url)

            self.assertTrue(Video.objects.filter(videourl__url=model_url).exists())

    def test_youtube_urls(self):
        self._test_urls(self.youtube_urls)

    def test_vimeo_urls(self):
        self._test_urls(self.vimeo_urls)

    def test_html5_urls(self):
        self._test_urls(self.html5_urls)

    def test_dailymotion_urls(self):
        self._test_urls(self.daily_motion_urls)


class TestTemplateTags(TestCase):
    def setUp(self):
        from django.conf import settings
        self.auth = {
            "username": u"admin",
            "password": u"admin"
        }
        fixture_path = os.path.join(settings.PROJECT_ROOT, "apps", "videos", "fixtures", "teams-list.json")
        data = json.load(open(fixture_path))
        self.videos = _create_videos(data, [])


    def test_language_url_for_empty_lang(self):
        v = self.videos[0]
        l = SubtitleLanguage(video=v, has_version=True)
        l.save()
        from videos.templatetags.subtitles_tags import language_url
        language_url(None, l)


class TestMetadataManager(TestCase):

    fixtures = ['staging_users.json', 'staging_videos.json']

    def test_language_count(self):
        video = Video.objects.all()[0]
        create_langs_and_versions(video, ['en'])
        metadata_manager.update_metadata(video.pk)
        video = Video.objects.all()[0]
        self.assertEqual(video.languages_count, 1)

def _create_trans( video, latest_version=None, lang_code=None, forked=False):
        translation = SubtitleLanguage()
        translation.video = video
        translation.language = lang_code
        translation.is_original = False
        translation.is_forked = forked
        if not forked:
            translation.standard_language = video.subtitle_language()
        translation.save()
        v = SubtitleVersion()
        v.language = translation
        if latest_version:
            v.version_no = latest_version.version_no+1
        else:
            v.version_no = 1
        v.datetime_started = datetime.now()
        v.save()

        if latest_version is not None:
            for s in latest_version.subtitle_set.all():
                s.duplicate_for(v).save()
        return translation

def create_version(lang, subs=None, user=None):
    latest = lang.latest_version()
    version_no = latest and latest.version_no + 1 or 1
    version = SubtitleVersion(version_no=version_no,
                              user=user or User.objects.all()[0],
                              language=lang,
                              datetime_started=datetime.now())
    version.is_forked = lang.is_forked
    version.save()
    if subs is None:
        subs = []
        for x in xrange(0,5):
            subs.append({
                "subtitle_text": "hey %s" % x,
                "subtitle_id": "%s-%s-%s" % (version_no, lang.pk, x),
                "start_time": x,
                "end_time": (x* 1.0) - 0.1
            })
    for sub in subs:
        s = Subtitle(**sub)
        s.version  = version
        s.save()
    return version


def refresh_obj(m):
    return m.__class__._default_manager.get(pk=m.pk)

class MarkupHtmlTest(TestCase):

    def test_markup_to_html(self):
        t = "there **bold text** there"
        self.assertEqual(
            "there <b>bold text</b> there",
            markup_to_html(t)
        )

    def test_html_to_markup(self):
        t = "there <b>bold text</b> there"
        self.assertEqual(
            "there **bold text** there",
            html_to_markup(t)
        )
def quick_add_subs(language, subs_texts, escape=True):
    subtitles = babelsubs.storage.SubtitleSet(language_code=language.language_code)
    for i,text in enumerate(subs_texts):
        subtitles.append_subtitle(i*1000, i*1000 + 999, text, escape=escape)
    add_subtitles(language.video, language.language_code, subtitles)
