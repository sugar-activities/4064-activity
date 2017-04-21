# Copyright 2007 World Wide Workshop Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
#
# If you find this activity useful or end up using parts of it in one of your
# own creations we would love to hear from you at info@WorldWideWorkshop.org !
#

# init gthreads before using abiword
import gobject
gobject.threads_init()

import os
import logging
import gtk

from gettext import gettext as _
import gettext
import locale

import hippo
from sugar.activity import activity

from globals import Globals
from gui.frame import Frame

import pages.choose
import pages.cover
import pages.joke
import pages.submit


# Mesh
import telepathy
import telepathy.client
from dbus import Interface
from dbus.service import method, signal
from dbus.gobject_service import ExportedGObject
from sugar.presence.tubeconn import TubeConnection # deprecated ?! Gone from build >542 ? Ke ?
from sugar.presence import presenceservice

from mesh.activitysession import JokeMachineSession, MESH_IFACE, MESH_PATH, MESH_SERVICE

# needed to unpickle state from journal
from persistence.jokemachinestate import JokeMachineState


# TODO: Handle <= Build# 622 gracefully 
try:
  from sugar.graphics.alert import NotifyAlert
except ImportError:
  pass



class JokeMachineActivity(activity.Activity):
  """Sugar activity for jokes

  The Joke Machine is a fiendishly clever device cooked up by the mad 
  scientists at the worldwide workshop for sharing jokes with your friends.

  If we have enough jokes we might even be able to make all the angry people
  in our world collapse with giggles of helpless laughter!
  """


  def __init__(self, handle):
    activity.Activity.__init__(self, handle)

    # TODO - clean -  install gettext
    os.chdir(Globals.pwd)  # required for i18n.py to work
    #gettext.install('JokeMachine', './po', unicode=True)
    #presLan_af = gettext.translation("JokeMachine", os.path.join(Globals.pwd, 'po'), languages=['af'])
    #presLan_af.install()
    #locale.setlocale(locale.LC_ALL, 'af')    

    # customize theme
    gtkrc = os.path.join(Globals.pwd, 'resources/gtkrc')
    if os.path.exists(gtkrc):
      logging.debug("Loading resources from %s" % gtkrc)
      gtk.rc_add_default_file(gtkrc)
      settings = gtk.settings_get_default()
      #gtk.rc_reset_styles(settings)
      gtk.rc_reparse_all_for_settings(settings, True)
      logging.debug("Loading resources DONE")

    Globals.set_activity_instance(self)

    logging.debug("Starting the Joke Machine activity")

    # toolbox
    self.__toolbox = activity.ActivityToolbox(self)
    self.set_toolbox(self.__toolbox)

    # main activity frame
    self.__activity_frame = Frame()
    vbox = gtk.VBox()
    vbox.pack_start(self.__activity_frame)
    vbox.show()
    self.set_canvas(vbox)
    self.show_all()

    # Initialize mesh ##########################################################

    # init Presence Service
    self.__presence_service = presenceservice.get_instance()
    try:
      name, path = self.__presence_service.get_preferred_connection()
      self.__telepathy_connection = telepathy.client.Connection(name, path)
      self.__telepathy_initiating = None
    except TypeError:
      logging.debug('Presence service offline')

    # Buddy object for you
    owner = self.__presence_service.get_owner()
    Globals.set_owner(owner)

    self.__session = None  # JokeMachineSession
    self.connect('shared', self.__do_activity_shared)


    # Check if we're joining another instance 
    self.__is_initiator = True
    if self._shared_activity is not None:
      self.alert(_('Joke Machine'), _('Please wait a moment for your buddy\'s Jokebooks to show up'))
      self.__is_initiator = False
      logging.debug('shared:  %s' % self._shared_activity.props.joined)
      # We are joining the activity
      logging.debug('Joined activity')                      
      self.connect('joined', self.__do_activity_joined) 
      self._shared_activity.connect('buddy-joined', self.__do_buddy_joined) 
      self._shared_activity.connect('buddy-left', self.__do_buddy_left) 
      if self.get_shared():
        # We've already joined
        self.__do_activity_joined()
    else:   
      logging.debug('Created activity')    

    # ##########################################################################

    # set default startup page if we're the initiator
    if self.is_initiator: 
      self.set_page(pages.choose.Choose)


  def alert(self, title, text=None):
    '''Show an alert above the activity.'''
    try:
      alert = NotifyAlert(timeout=10)
    except NameError:
      logging.warning('sugar.graphics.alert.NotifyAlert not present for %r, %r', title, text)
      return
    alert.props.title = title
    alert.props.msg = text
    self.add_alert(alert)
    alert.connect('response', self.__alert_cancel_cb)
    alert.show()
    
  def __alert_cancel_cb(self, alert, response_id):
    '''Callback for alert events'''
    self.remove_alert(alert)


  # Mesh Callbacks #############################################################

  def __setup(self):
    '''Setup the Tubes channel
    Called from: __do_activity_shared, __do_activity_joined.'''

    if self._shared_activity is None:
      logging.error('Failed to share or join activity')
      return

    bus_name, conn_path, channel_paths = self._shared_activity.get_channels()

    # Work out what our room is called and whether we have Tubes already
    room = None
    tubes_chan = None
    text_chan = None
    for channel_path in channel_paths:
      channel = telepathy.client.Channel(bus_name, channel_path)
      htype, handle = channel.GetHandle()
      if htype == telepathy.HANDLE_TYPE_ROOM:
        logging.debug('Found our room: it has handle# %d %s', 
                      handle, 
                      self.__telepathy_connection.InspectHandles(htype, [handle])[0])
        room = handle
        ctype = channel.GetChannelType()
        if ctype == telepathy.CHANNEL_TYPE_TUBES:
          logging.debug('Found our Tubes channel at %s', channel_path)
          tubes_chan = channel
        elif ctype == telepathy.CHANNEL_TYPE_TEXT:
          logging.debug('Found our Text channel at %s', channel_path)
          text_chan = channel

    if room is None:
      logging.debug('Presence service did not create a room')
      return
    if text_chan is None:
      logging.debug('Presence service did not create a text channel')
      return
    if tubes_chan is None:
      logging.debug('Presence service did not create a tubes channel')
      # okay - we're going to try requesting one because this always fails on
      # build# <= 622
      logging.debug('TODO - DEPRECATE: TRYING TO REQUEST A TUBES CHANNEL')
      tubes_chan = self.__telepathy_connection.request_channel(telepathy.CHANNEL_TYPE_TUBES,
                                                               telepathy.HANDLE_TYPE_ROOM, 
                                                               room, 
                                                               True)
      if tubes_chan is None:
        logging.debug('TODO - DEPRECATE: FAILED TO REQUEST A TUBES CHANNEL - QUITTING')
        return
      logging.debug('TODO - DEPRECATE: MANAGED TO REQUEST A TUBES CHANNEL')
      
    self.tubes_chan = tubes_chan
    self.text_chan = text_chan

    tubes_chan[telepathy.CHANNEL_TYPE_TUBES].connect_to_signal('NewTube', self._new_tube_cb)


  def __do_activity_joined(self, activity):
    pass
    '''Callback for completion of joining the activity.'''
    if not self._shared_activity:
      return

    # Find out who's already in the shared activity:
    for buddy in self._shared_activity.get_joined_buddies():
      logging.debug('Buddy %s is already in the activity' % buddy.props.nick)

    logging.debug('Joined an existing shared activity')
    self.__telepathy_initiating = False
    self.__setup()

    logging.debug('This is not my activity: waiting for a tube...')
    self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES].ListTubes(
      reply_handler=self._list_tubes_reply_cb,
      error_handler=self._list_tubes_error_cb)


  def _new_tube_cb(self, id, initiator, type, service, params, state):
    '''Callback for when we have a Tube.'''
    logging.debug('New tube: ID=%d initator=%d type=%d service=%s params=%r state=%d', 
                  id, initiator, type, service, params, state)

    if (type == telepathy.TUBE_TYPE_DBUS and service == MESH_SERVICE):
      if state == telepathy.TUBE_STATE_LOCAL_PENDING:
        self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES].AcceptDBusTube(id)

      tube_conn = TubeConnection(self.__telepathy_connection,
                                 self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES],
                                 id, 
                                 group_iface=self.text_chan[telepathy.CHANNEL_INTERFACE_GROUP])
      
      logging.info('Starting a new JokeMachineSession')
      self.__session = JokeMachineSession(tube_conn, self.__telepathy_initiating, self._get_buddy, self) 


  def _get_buddy(self, cs_handle):
    """Get a Buddy from a channel specific handle."""
    logging.debug('Trying to find owner of handle %u...', cs_handle)
    group = self.text_chan[telepathy.CHANNEL_INTERFACE_GROUP]
    my_csh = group.GetSelfHandle()
    logging.debug('My handle in that group is %u', my_csh)
    if my_csh == cs_handle:
      handle = self.__telepathy_connection.GetSelfHandle()
      logging.debug('CS handle %u belongs to me, %u', cs_handle, handle)
    elif group.GetGroupFlags() & telepathy.CHANNEL_GROUP_FLAG_CHANNEL_SPECIFIC_HANDLES:
      handle = group.GetHandleOwners([cs_handle])[0]
      logging.debug('CS handle %u belongs to %u', cs_handle, handle)
    else:
      handle = cs_handle
      logging.debug('non-CS handle %u belongs to itself', handle)
      assert handle != 0

    name, path = self.__presence_service.get_preferred_connection() 

    return self.__presence_service.get_buddy_by_telepathy_handle(name,
                                                                 path, 
                                                                 handle)


  def __do_buddy_joined(self, activity, buddy):
    logging.debug('Buddy %s joined' % buddy.props.nick)


  def __do_buddy_left(self, activity, buddy):
    logging.debug('Buddy %s left' % buddy.props.nick)


  def __do_activity_shared(self, activity):
    '''Callback for completion of sharing of activity'''
    logging.debug('The activity was shared')
    
    self.__telepathy_initiating = True
    self.__setup() 

    for buddy in self._shared_activity.get_joined_buddies():
      logging.debug('Buddy %s is already in the activity' % buddy.props.nick)

    self._shared_activity.connect('buddy-joined', self.__do_buddy_joined)
    self._shared_activity.connect('buddy-left', self.__do_buddy_left)

    logging.debug('This is my activity: making a tube...')
    id = self.tubes_chan[telepathy.CHANNEL_TYPE_TUBES].OfferDBusTube(MESH_SERVICE, {})


  def _list_tubes_reply_cb(self, tubes):
    for tube_info in tubes:
      self._new_tube_cb(*tube_info)

  def _list_tubes_error_cb(self, e):
    logging.error('ListTubes() failed: %s', e)


  @property
  def tube(self):
    logging.debug('Getting tube for activity: %r', self.__session)
    return self.__session # TODO rename :-)

  @property 
  def is_shared(self):
    ret = self.__session is not None
    logging.debug('Getting is_shared for activity: %r', ret)
    return ret
  
  @property
  def is_initiator(self):
    '''True if I'm the one joining an activity which was shared by someone else'''
    ret = self.__is_initiator 
    logging.debug('Getting is_initiator for activity: %r', ret)
    return ret

  # ############################################################################


  def refresh(self):
    '''reload the current page'''
    page_class = self.__activity_frame.page_class
    logging.debug('Refreshing Page %r' % page_class)
    self.set_page(page_class)


  # TODO -> Make generally cleverer
  # TODO -> Cache constructed pages if necessary for performance
  # TODO -> Handle multiple page constructor arguments
  def set_page(self, page_class, *args): 
    page = page_class(*args)
    self.__activity_frame.page = page
    return page


  #def alert(self, title, text=None):
    #'''Show an alert above the activity.'''
    #alert = NotifyAlert(timeout=10)
    #alert.props.title = title
    #alert.props.msg = text
    #self.add_alert(alert)
    #alert.connect('response', self.__alert_cancel_cb)
    #alert.show()


  #def __alert_cancel_cb(self, alert, response_id):
    #'''Callback for alert events'''
    #self.remove_alert(alert)
    

  def read_file(self, file_path):
    '''Callback to resume activity state from Journal'''
    logging.debug('Reading file from datastore via Journal: %s' % file_path)
    f = open(file_path, 'r')
    pickle = f.read()
    if len(pickle) == 0:
      logging.debug('Activity.read_file() -> Journal has empty pickle - creating empty state')
      activity_state = JokeMachineState().test_data()
    else:
      logging.debug('Unpickling state from Journal')
      activity_state = JokeMachineState.loads(pickle)
    f.close()

    # set Globals.ActivityState
    Globals.set_activity_state(activity_state) 


  def write_file(self, file_path):
    '''Callback to persist activity state to Journal'''
    
    if len(Globals.JokeMachineState.jokebooks) != 0:
      logging.debug('Writing file to datastore via Journal: %s' % file_path)
      f = open(file_path, 'w')
      pickle = Globals.JokeMachineState.dumps()
      f.write(pickle)
      f.close()
    else:
      logging.debug('nothing to persist')
      raise NotImplementedError


  def can_close(self):    
    '''Called on activity close'''
    logging.info('Exiting Activity. Performing cleanup...')
    Globals.shutdown()
    logging.info('Done')
    return True
