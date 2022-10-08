from ..game import Mode
from ..game import SwitchContinue
import logging

class Trough(Mode):
    """Manages trough by providing the following functionality:

        - Keeps track of the number of balls in play.
        - Keeps track of the number of balls in the trough.
        - Keeps track of each launch request separately and executes them in chronological order
        - Launches one or more balls and calls a launch_callback (if one exists) when all balls have ejected for that launch request
        - calls a launched_callback (if one exists) when all launch requests are done and the last ejected ball actually made it to the shooter lane.
        - Auto-launches balls while ball save is active (if linked to a ball save object).
        - Identifies when balls drain and calls a registered drain_callback (if one exists).
        - Maintains a count of balls locked in playfield lock features (if externally incremented) and
          adjusts the count of number of balls in play appropriately.
          This will help the drain_callback distinguish between a ball ending or simply a multiball ball draining.

        Changes beyond the standard PyProcGame Trough:
            - keep track of the launch_callback per launch request, launch_callback when used must be passed to :meth:launch_balls()
            - supports autoplunge (see :meth:launch_and_autoplunge_balls()) if 'plunge_coilname' is provided to init()
            - tries to ensure a ball has successfully escaped the shooter lane before plunging another (see 'shooter_lane_inactivity_time')

        Notes: launch_callback => called when the last ball to launch in the current launch request has ejected and is on its way to the shooter lane,
                                  passed as an argument to :meth:launch_balls()
               launched_callback => called when all launch requests are done and the last ejected ball actually made it to the shooter lane,
                                  set externally

    Parameters:

        'game': Parent game object.
        'position_switchnames': List of switchnames for each ball position in the trough.
        'eject_switchname': Name of switch in the ball position the feeds the shooter lane.
        'eject_coilname': Name of coil used to put a ball into the shooter lane.
        'early_save_switchnames': List of switches that will initiate a ball save before the draining ball reaches the trough (ie. Outlanes).
        'shooter_lane_switchname': Name of the switch in the shooter lane.  This is checked before a new ball is ejected.
        'drain_callback': Optional - Name of method to be called when a ball drains (and isn't saved).
        'shooter_lane_inactivity_time': Optional - The amount of time the shooter lane should turn inactive
            to imply a ball has been successfully launched and is away (i.e., failed plunge takes less than this time)
        'plunge_coilname': Optional - Name of a coil to be fired to autoplunge a ball if launch_and_autoplunge_balls() is called.
    """
    def __init__(self, game, position_switchnames, eject_switchname, eject_coilname, \
                     early_save_switchnames, shooter_lane_switchname, drain_callback=None,
                     shooter_lane_inactivity_time=2.0, plunge_coilname=None, autoplunge_settle_time=0.3, \
                     trough_settle_time=0.5):
        super(Trough, self).__init__(game, 90)
        self.logger = logging.getLogger('trough')

        self.position_switchnames = position_switchnames
        self.eject_switchname = eject_switchname
        self.eject_coilname = eject_coilname
        self.shooter_lane_switchname = shooter_lane_switchname
        self.drain_callback = drain_callback
        self.inactive_shooter_time = shooter_lane_inactivity_time
        self.plunge_coilname = plunge_coilname
        self.settle_time = trough_settle_time

        # if there is an outhole, add an auto-kickover
        outhole_sw_name = None
        if('outhole' in self.game.switches):
            outhole_sw_name = 'outhole'
        else:
            sa = self.game.switches.items_tagged('outhole')
            if(type(sa) is list and len(sa)==0):
                self.logger.info("No outhole switch found (name or tag).  If an outhole trough setup is preset, you should adjust names/tag in the machine yaml.")
            elif(type(sa) is list):
                outhole_sw_name = sa[0].name
                self.logger.warning("Multiple switches have been tagged 'outhole' -- since that makes no sense, only the first will be used.")
            else:
                outhole_sw_name = sa.name

        # at the point that there is an outhole switch, we need to find the outhole coil
        if(outhole_sw_name is not None):
            # find an outhole coilname
            self.outhole_coil = None
            if('outhole' in self.game.coils):
                self.outhole_coil = self.game.coils['outhole']
            else:
                sa = self.game.coils.items_tagged('outhole')
                if(type(sa) is list and len(sa)==0):
                    raise ValueError, "Outhole switch found but no 'outhole' coil found (name or tag).  If an outhole trough setup is preset, you should adjust names/tag in the machine yaml for switch and coil!"
                elif(type(sa) is list):
                    self.outhole_coil = sa[0]
                    self.logger.warning("Multiple coils have been tagged 'outhole' -- since that makes no sense, only the first will be used.")
                else:
                    self.outhole_coil = sa

            if(self.outhole_coil is not None):
                self.add_switch_handler(name=outhole_sw_name, event_type='active',\
                    delay=0.3, handler=self.outhole_handler)

        # Install switch handlers.
        # Use a delay of 750ms which should ensure balls are settled.
        for switch in position_switchnames:
            self.add_switch_handler(name=switch, event_type='active', \
                delay=None, handler=self.position_switch_handler)

        for switch in position_switchnames:
            self.add_switch_handler(name=switch, event_type='inactive', \
                delay=None, handler=self.position_switch_handler)

        # Install early ball_save switch handlers.
        for switch in early_save_switchnames:
            self.add_switch_handler(name=switch, event_type='active', \
                delay=None, handler=self.early_save_switch_handler)

        # install "successful feed" switch handler, note the auto_plunge_settle_time is the rest time
        self.add_switch_handler(name=shooter_lane_switchname, event_type='active', \
                delay=autoplunge_settle_time, handler=self.ball_in_shooterlane)

        for sw in self.game.switches.items_tagged('troughJam'):
            #This switch handler will trigger every time the jam opto is active for 2 seconds
            self.add_switch_handler(name=sw.name, event_type='active', delay=2, handler=self.jam_opto_handler)

        # Reset variables
        self.launch_requests = []
        self.num_balls_in_play = 0
        self.num_balls_locked = 0
        self.num_balls_to_launch = 0    # total number to be launched (including stealth balls)
        self.eject_in_progress = False

        self.ball_save_active = False

        """ Callback called when a ball is saved.  Used optionally only when ball save is enabled
        (by a call to :meth:`Trough.enable_ball_save`).  Set externally if a callback should be used. """
        self.ball_save_callback = None

        """ Method to get the number of balls to save.  Set externally when using ball save logic."""
        self.num_balls_to_save = None

        """ Method to get whether the ball saver allows multiple saves.  Set externally when using ball save logic."""
        self.allow_multiple_saves = None

        """ Callback called when all launch requests are done and the last ejected ball actually made it to the shooter lane"""
        self.launched_callback = None

        #self.debug()

    def outhole_handler(self, sw):
        """ a method to auto pulse the outhole coil when the outhole switch is closed for a sufficiently
            long enough time for the ball to settle.  This is hard coded to 300ms but should almost certainly
            be programmatic... -- note, this method will be registered if the machine yaml includes a
            switch named outhole (or tag:outhole) and a coil named (or tagged) outhole.  Since the trough
            logic is based on the trough switches themselves, all this switch needs to do is move a ball
            into the trough for proper handling.  Since modern machines may not have an outhole trough setup,
            it is not an error to not have an outhole switch/coil pair.
        """
        if(self.outhole_coil is not None):
            self.outhole_coil.pulse()
        return SwitchContinue

    def jam_opto_handler(self, sw):
        self.logger.info("detected ball jam, ejecting a ball")
        self.game.coils[self.eject_coilname].pulse(self.game.coils[self.eject_coilname].default_pulse_time - 5)

    def debug(self):
        self.logger.debug("num balls: %d ; balls in play: %d, balls locked: %d" % (self.num_balls(), self.num_balls_in_play, self.num_balls_locked))
        self.delay(name='debug', event_type=None, delay=1.0, handler=self.debug)

    def enable_ball_save(self, enable=True):
        """Used to enable/disable ball save logic."""
        self.ball_save_active = enable

    def early_save_switch_handler(self, sw):
        if self.ball_save_active:
            # Only do an early ball save if a ball is ready to be launched.
            # Otherwise, let the trough switches take care of it.
            if self.num_balls() > 0:
                self.launch_balls(1, self.ball_save_callback, stealth=True)

    def mode_stopped(self):
        self.cancel_delayed('check_switches')

    # Switches will change states a lot as balls roll down the trough.
    # So don't go through all of the logic every time.  Keep resetting a
    # delay function when switches change state.  When they're all settled,
    # the delay will call the real handler (check_switches).
    def position_switch_handler(self, sw):
        self.cancel_delayed('check_switches')
        self.delay(name='check_switches', event_type=None, delay=self.settle_time, handler=self.check_switches)

    def check_switches(self):
        # we can confirm the ball counts even if a launch is in progress,
        # but not if a ball is on its way to the shooter lane
        if self.num_balls_in_play > 0 and not self.eject_in_progress:
            # Base future calculations on how many balls the machine
            # thinks are currently installed.
            num_installed_balls = self.game.num_balls_total
            curr_trough_count = self.num_balls()
            if self.ball_save_active:

                if self.num_balls_to_save:
                    num_balls_to_save = self.num_balls_to_save()
                else:
                    num_balls_to_save = 0

                # Calculate how many balls shouldn't be in the
                # trough assuming one just drained
                num_balls_out = self.num_balls_locked + (num_balls_to_save - 1)

                # Translate that to how many balls should be in
                # the trough if one is being saved.
                expected_trough_count = num_installed_balls - num_balls_out

                if (curr_trough_count - self.num_balls_to_launch) >= expected_trough_count:
                    if self.allow_multiple_saves and self.allow_multiple_saves():
                        num_balls_saved = curr_trough_count - self.num_balls_to_launch - expected_trough_count + 1
                    else:
                        num_balls_saved = 1
                        # disable the ball save right away since another ball could drain
                        # before the ball_save_callback is executed
                        self.ball_save_active = False
                    self.logger.info("Saving %d balls" % num_balls_saved)
                    for unused in range(0, num_balls_saved):
                        # create individual launch requests, we want to call the callback for each ball
                        self.launch_balls(1, self.ball_save_callback, stealth=True)
                else:
                    # If there are too few balls in the trough.
                    # Ignore this one in an attempt to correct the tracking.
                    self.logger.warning("expected to have more balls than current; not launching [curr trough count=%d - pending=%d] < [expected_trough_count=%d] --retry in 1s" % (curr_trough_count, self.num_balls_to_launch, expected_trough_count))
                    return 'ignore'
            else:
                # Calculate how many balls should be in the trough
                # for various conditions.
                num_trough_balls_if_ball_ending = \
                    num_installed_balls - self.num_balls_locked
                num_trough_balls_if_multiball_ending = \
                    num_trough_balls_if_ball_ending - 1
                num_trough_balls_if_multiball_drain = \
                    num_trough_balls_if_ball_ending - \
                    (self.num_balls_in_play - 1)

                # The ball should end if all of the balls
                # are in the trough.
                if curr_trough_count == num_installed_balls or \
                   curr_trough_count == num_trough_balls_if_ball_ending:
                    self.num_balls_in_play -= 1
                    if self.drain_callback:
                        self.drain_callback()

                    # it's possible that multiple balls have
                    # drained since the last time we checked (due to
                    # ball settling delay); if so, the num balls in
                    # play will not be zero yet (but we do want to 
                    # fire the callback for every ball in the trough);
                    # Use a delay to check the trough again in 1s
                    # since the now settled balls won't raise a new
                    # switch event.
                    if(self.num_balls_in_play > 0):
                        self.delay(delay=1, handler=self.check_switches)

                # Multiball is ending if all but 1 ball are in the trough.
                # Shouldn't need this, but it fixes situations where
                # num_balls_in_play tracking
                # fails, and those situations are still occurring.
                elif curr_trough_count == \
                     num_trough_balls_if_multiball_ending:
                    self.num_balls_in_play = 1
                    if self.drain_callback:
                        self.drain_callback()
                # Otherwise, another ball from multiball is draining
                # if the trough gets one more than it would have if
                # all num_balls_in_play are not in the trough.
                elif curr_trough_count ==  \
                     num_trough_balls_if_multiball_drain:
                    # Fix num_balls_in_play if too low.
                    if self.num_balls_in_play < 3:
                        self.num_balls_in_play = 2
                    # otherwise subtract 1
                    else:
                        self.num_balls_in_play -= 1
                    if self.drain_callback:
                        self.drain_callback()
                else:
                    # a ball has drained _but_ this isn't the end of ball
                    # or the last multiball.  by checking to make sure that
                    # we expect additional balls are in play, we suppress
                    # messages from someone pulling balls out of the trough
                    # or too many balls installed in the machine
                    if(self.num_balls_in_play > 1):
                        self.drain_callback()
        else: # there are no balls in play...
            if(self.is_full() and self.game.game_start_pending):
                self.game.your_search_is_over()
            elif(self.game.game_tilted):
                self.drain_callback()

    # Count the number of balls in the trough by counting active trough switches.
    def num_balls(self):
        """Returns the number of balls in the trough."""
        ball_count = 0
        for switch in self.position_switchnames:
            if self.game.switches[switch].is_active():
                ball_count += 1
        return ball_count

    def is_full(self):
        return self.num_balls() == self.game.num_balls_total

    def launch_and_autoplunge_balls(self, num):
        if(self.plunge_coilname is None):
            raise ValueError, "trough cannot autoplunge when no autoplunge coil is defined!"
        self.launch_balls(num, autoplunge=True)


    # Create a new launch request and queue it for execution.
    # Keep a count of the number of balls to launch summed over all pending launch requests.
    # Also keep a separate count of the number stealth balls to launch summed
    # over all pending launch requests. Stealth balls do not increase num_balls_in_play.
    # If no launch request is executing, initiate this launch request.
    def launch_balls(self, num, callback=None, stealth=False, autoplunge=False):
        """Launches balls into play.

            'num': Number of balls to be launched.
            If ball launches are still pending from a previous request,
            this number will be added to the previously requested number.

            'callback': If specified, the callback will be called once
            all of the requested balls have been launched.

            'stealth': Set to true if the balls being launched should NOT
            be added to the number of balls in play.  For instance, if
            a ball is being locked on the playfield, and a new ball is
            being launched to keep only 1 active ball in play,
            stealth should be used.

            'autoplunge': Set to true to autoplunge the balls.
            Ignored for stealth launches since stealth balls
            are always autoplunged
        """

        self.num_balls_to_launch += num
        self.logger.info("launch balls num=%d stealth=%s autoplunge=%s [pending=%d]" % (num, stealth, autoplunge, self.num_balls_to_launch))

        launch_request = LaunchRequest(num, callback, stealth, autoplunge)
        self.launch_requests.append(launch_request)
        if len(self.launch_requests) == 1:
            # there are no launches in progress, start executing the request right away
            self.common_launch_code()

    # This is the part of the ball launch code that repeats for multiple launches.
    def common_launch_code(self):
        # Only kick out another ball if the last ball is gone from the shooter lane.
        if self.game.switches[self.shooter_lane_switchname].is_active() or \
           self.game.switches[self.shooter_lane_switchname].time_since_change() < self.inactive_shooter_time:
            # Wait before trying again but keep the delay tight to speed up multiball launches
            delay = 1.0 if self.game.switches[self.shooter_lane_switchname].is_active() else \
                self.inactive_shooter_time - self.game.switches[self.shooter_lane_switchname].time_since_change() + 0.001
            self.logger.info("Cannot feed ball as shooter lane isn't ready [pending=%d] --retry in %f sec" % (self.num_balls_to_launch, delay))
            self.delay(name='launch', event_type=None, delay=delay, handler=self.common_launch_code)
        elif(self.num_balls()<1):
            self.logger.info("Cannot feed ball as trough is empty! [pending=%d] -- wait for ball drain" % (self.num_balls_to_launch))
            # we don't do anything else, because the trough handler will auto-call this when a ball drains
        else:
            # feed the shooter lane (via trough coil)
            self.eject_in_progress = True
            self.logger.debug("Feeding ball to shooter lane. [pending=%d]" % (self.num_balls_to_launch))
            self.game.coils[self.eject_coilname].pulse()

            # we'll try again if the ball takes too long to reach the shooter lane
            self.delay(name='ejectErrorWatch', delay=4, handler=self.ball_eject_error)

            # if this is the last ball to be launched for this launch request,
            # notify that the ball has been ejected and is on its way to the shooter lane
            if self.launch_requests[0].launch_callback and self.launch_requests[0].num_balls_to_launch==1:
                self.launch_requests[0].launch_callback()

    def num_balls_out(self):
        """ returns the number of balls that are currently not in the trough.
            NOT including balls pending launch, but does include locked balls """
        curr_trough_count = self.num_balls()
        return (self.game.num_balls_total - curr_trough_count)

    def num_balls_requested(self):
        """ returns the number of balls that will be eventually "live", counted as the number of live
            balls currently plus the number of pending non-stealth ejects """
        return self.num_balls_in_play + sum([req.num_balls_to_launch if not req.stealth else 0 for req in self.launch_requests])

    def ball_in_shooterlane(self, sw):
        # eject was successful, cancel ball eject error watch
        self.cancel_delayed('ejectErrorWatch')

        if (self.eject_in_progress):
            self.num_balls_to_launch -= 1
            self.launch_requests[0].num_balls_to_launch -= 1

            # Stealth balls do not modify the number of balls in play
            if not self.launch_requests[0].stealth:
                self.num_balls_in_play += 1

            self.logger.debug("Fed ball to shooter lane. [pending=%d]" % self.num_balls_to_launch)
            self.eject_in_progress = False

            if(self.launch_requests[0].autoplunge and self.plunge_coilname is not None):
                self.logger.info("Autoplunging ball")
                self.game.coils[self.plunge_coilname].pulse()

            if self.launch_requests[0].num_balls_to_launch == 0:
                # this launch request is fully served, start serving the next request if there is one
                self.launch_requests.pop(0)

            if self.launch_requests:
                # there are still some pending launch requests, delay to continue executing them
                self.delay(name='launch', event_type=None, delay=self.inactive_shooter_time, \
                   handler=self.common_launch_code)
            else:
                # all launch requests done, fire this to notify all balls have made it to the shooter lane 
                if self.launched_callback:
                    self.launched_callback()

    def ball_eject_error(self):
        if self.eject_in_progress == True:
            # make extra sure there is no ball in shooter lane before we retry the launch
            if self.game.switches[self.shooter_lane_switchname].is_inactive(self.inactive_shooter_time):
                self.logger.info("ball eject error watch elapsed, ejecting the ball again")
                self.game.coils[self.eject_coilname].pulse()
            self.delay(name='ejectErrorWatch', delay=4, handler=self.ball_eject_error)

class LaunchRequest(object):
    """A data object that remembers the details of a single call to launch_balls"""
    def __init__(self, num_balls_to_launch, launch_callback, stealth, autoplunge):
        self.num_balls_to_launch = num_balls_to_launch
        self.stealth = stealth
        self.autoplunge = stealth or autoplunge
        self.launch_callback = launch_callback
