# Amara, universalsubtitles.org
#
# Copyright (C) 2014 Participatory Culture Foundation
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

from optparse import make_option

from django.conf import settings
from sslserver.management.commands import runsslserver

from staticmedia import utils

settings.STATIC_URL = utils.static_url()

# hack to patch the runsslserver command.  We want to set use_static_handler
# to false, but there's not actually an option to do it.  So we create the
# option ourself

runsslserver.Command.option_list += (
        make_option("--nostatic", dest='use_static_handler',
                    action='store_false', default=True),
)
