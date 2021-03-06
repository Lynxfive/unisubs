# Amara, universalsubtitles.org
#
# Copyright (C) 2013 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.
from django import template
from django.conf import settings
from rosetta.views import can_translate as can_translate_func

from auth.models import CustomUser as User
from profiles.forms import SelectLanguageForm
from utils.translation import get_user_languages_from_request, get_user_languages_from_cookie
from videos.models import Action


register = template.Library()

ACTIONS_ON_PAGE = getattr(settings, 'ACTIONS_ON_PAGE', 10)

@register.filter
def can_translate(user):
    return can_translate_func(user)

@register.inclusion_tag('profiles/_require_email_dialog.html', takes_context=True)
def require_email_dialog(context):
    return context

@register.inclusion_tag('profiles/_select_language_dialog.html', takes_context=True)
def select_language_dialog(context, option=None, hide_link=False, redirect=None, display_only=False, show_current=False):

    user_langs = get_user_languages_from_request(context['request'], readable=True, guess=False)
    current = {}

    for i, l in enumerate(user_langs):
        current['language%s' % (i+1)] = l

    return {
        'current': current,
        'force_ask': (option == 'force') and _user_needs_languages(context),
        'hide_link': hide_link,
        'redirect': redirect,
        'show_current': show_current,
        'request':context['request']
    }

@register.inclusion_tag('profiles/_select_language_form.html', takes_context=True)
def select_language_form(context):
    user_langs = get_user_languages_from_request(context['request'], guess=False)
    initial_data = {}

    for i, l in enumerate(user_langs):
        initial_data['language%s' % (i+1)] = l

    form = SelectLanguageForm(initial=initial_data)

    return {
        'form': form,
        'redirect' : 'redirect' in context and context['redirect']
    }

def _user_needs_languages(context):
    user = context['user']
    if user.is_authenticated():
        return not user.userlanguage_set.exists()
    else:
        return not bool(get_user_languages_from_cookie(context['request']))

@register.inclusion_tag('profiles/_user_videos_activity.html', takes_context=True)
def user_videos_activity(context, user=None):
    user = user or context['user']

    if user.is_authenticated():
        context['users_actions'] = Action.objects.select_related('video', 'language', 'language__video', 'user') \
            .filter(video__customuser=user) \
            .exclude(user=user) \
            .exclude(user=User.get_anonymous())[:ACTIONS_ON_PAGE]
    else:
        context['users_actions'] = Action.objects.none()
    return context


@register.inclusion_tag('profiles/_user_avatar.html', takes_context=True)
def user_avatar(context, user_obj):
    return {
        'user': context['user'],
        'user_obj':user_obj
    }

@register.filter
def custom_avatar(user, size):
    return user._get_avatar_by_size(size)

@register.inclusion_tag('profiles/_top_user_panel.html', takes_context=True)
def top_user_panel(context):
    return context
