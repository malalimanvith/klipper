# Interface to Klipper micro-controller code
#
# Copyright (C) 2016,2017  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys, os, zlib, logging, math
import serialhdl, pins, chelper, clocksync

class error(Exception):
    pass

STEPCOMPRESS_ERROR_RET = -989898989

class MCU_stepper:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._oid = self._mcu.create_oid()
        self._step_pin = pin_params['pin']
        self._invert_step = pin_params['invert']
        self._dir_pin = self._invert_dir = None
        self._commanded_pos = 0
        self._step_dist = self._inv_step_dist = 1.
        self._velocity_factor = self._accel_factor = 0.
        self._mcu_position_offset = 0
        self._mcu_freq = self._min_stop_interval = 0.
        self._reset_cmd = self._get_position_cmd = None
        self._ffi_lib = self._stepqueue = None
    def get_mcu(self):
        return self._mcu
    def setup_dir_pin(self, pin_params):
        if pin_params['chip'] is not self._mcu:
            raise pins.error("Stepper dir pin must be on same mcu as step pin")
        self._dir_pin = pin_params['pin']
        self._invert_dir = pin_params['invert']
    def setup_min_stop_interval(self, min_stop_interval):
        self._min_stop_interval = min_stop_interval
    def setup_step_distance(self, step_dist):
        self._step_dist = step_dist
        self._inv_step_dist = 1. / step_dist
    def build_config(self):
        self._mcu_freq = self._mcu.get_mcu_freq()
        self._velocity_factor = 1. / (self._mcu_freq * self._step_dist)
        self._accel_factor = 1. / (self._mcu_freq**2 * self._step_dist)
        max_error = self._mcu.get_max_stepper_error()
        min_stop_interval = max(0., self._min_stop_interval - max_error)
        self._mcu.add_config_cmd(
            "config_stepper oid=%d step_pin=%s dir_pin=%s"
            " min_stop_interval=%d invert_step=%d" % (
                self._oid, self._step_pin, self._dir_pin,
                self._mcu.seconds_to_clock(min_stop_interval),
                self._invert_step))
        step_cmd = self._mcu.lookup_command(
            "queue_step oid=%c interval=%u count=%hu add=%hi")
        dir_cmd = self._mcu.lookup_command(
            "set_next_step_dir oid=%c dir=%c")
        self._reset_cmd = self._mcu.lookup_command(
            "reset_step_clock oid=%c clock=%u")
        self._get_position_cmd = self._mcu.lookup_command(
            "stepper_get_position oid=%c")
        ffi_main, self._ffi_lib = chelper.get_ffi()
        self._stepqueue = ffi_main.gc(self._ffi_lib.stepcompress_alloc(
            self._mcu.seconds_to_clock(max_error), step_cmd.msgid, dir_cmd.msgid,
            self._invert_dir, self._oid),
                                      self._ffi_lib.stepcompress_free)
        self._mcu.register_stepqueue(self._stepqueue)
    def get_oid(self):
        return self._oid
    def set_position(self, pos):
        if pos >= 0.:
            steppos = int(pos * self._inv_step_dist + 0.5)
        else:
            steppos = int(pos * self._inv_step_dist - 0.5)
        self._mcu_position_offset += self._commanded_pos - steppos
        self._commanded_pos = steppos
    def get_commanded_position(self):
        return self._commanded_pos * self._step_dist
    def get_mcu_position(self):
        return self._commanded_pos + self._mcu_position_offset
    def note_homing_start(self, homing_clock):
        ret = self._ffi_lib.stepcompress_set_homing(
            self._stepqueue, homing_clock)
        if ret:
            raise error("Internal error in stepcompress")
    def note_homing_finalized(self):
        ret = self._ffi_lib.stepcompress_set_homing(self._stepqueue, 0)
        if ret:
            raise error("Internal error in stepcompress")
        ret = self._ffi_lib.stepcompress_reset(self._stepqueue, 0)
        if ret:
            raise error("Internal error in stepcompress")
    def note_homing_triggered(self):
        params = self._mcu.serial.send_with_response(
            self._get_position_cmd.encode(self._oid),
            'stepper_position', self._oid)
        pos = params['pos']
        if self._invert_dir:
            pos = -pos
        self._mcu_position_offset = pos - self._commanded_pos
    def reset_step_clock(self, print_time):
        clock = self._mcu.print_time_to_clock(print_time)
        ret = self._ffi_lib.stepcompress_reset(self._stepqueue, clock)
        if ret:
            raise error("Internal error in stepcompress")
        data = (self._reset_cmd.msgid, self._oid, clock & 0xffffffff)
        ret = self._ffi_lib.stepcompress_queue_msg(
            self._stepqueue, data, len(data))
        if ret:
            raise error("Internal error in stepcompress")
    def step(self, print_time, sdir):
        clock = print_time * self._mcu_freq
        count = self._ffi_lib.stepcompress_push(self._stepqueue, clock, sdir)
        if count == STEPCOMPRESS_ERROR_RET:
            raise error("Internal error in stepcompress")
        self._commanded_pos += count
    def step_const(self, print_time, start_pos, dist, start_v, accel):
        clock = print_time * self._mcu_freq
        inv_step_dist = self._inv_step_dist
        step_offset = self._commanded_pos - start_pos * inv_step_dist
        count = self._ffi_lib.stepcompress_push_const(
            self._stepqueue, clock, step_offset, dist * inv_step_dist,
            start_v * self._velocity_factor, accel * self._accel_factor)
        if count == STEPCOMPRESS_ERROR_RET:
            raise error("Internal error in stepcompress")
        self._commanded_pos += count
    def step_delta(self, print_time, dist, start_v, accel
                   , height_base, startxy_d, arm_d, movez_r):
        clock = print_time * self._mcu_freq
        inv_step_dist = self._inv_step_dist
        height = self._commanded_pos - height_base * inv_step_dist
        count = self._ffi_lib.stepcompress_push_delta(
            self._stepqueue, clock, dist * inv_step_dist,
            start_v * self._velocity_factor, accel * self._accel_factor,
            height, startxy_d * inv_step_dist, arm_d * inv_step_dist, movez_r)
        if count == STEPCOMPRESS_ERROR_RET:
            raise error("Internal error in stepcompress")
        self._commanded_pos += count

class MCU_endstop:
    error = error
    RETRY_QUERY = 1.000
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._steppers = []
        self._pin = pin_params['pin']
        self._pullup = pin_params['pullup']
        self._invert = pin_params['invert']
        self._cmd_queue = mcu.alloc_command_queue()
        self._oid = self._home_cmd = self._query_cmd = None
        self._homing = False
        self._min_query_time = 0.
        self._next_query_clock = self._home_timeout_clock = 0
        self._retry_query_ticks = 0
        self._last_state = {}
    def get_mcu(self):
        return self._mcu
    def add_stepper(self, stepper):
        self._steppers.append(stepper)
    def build_config(self):
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd(
            "config_end_stop oid=%d pin=%s pull_up=%d stepper_count=%d" % (
                self._oid, self._pin, self._pullup, len(self._steppers)))
        for i, s in enumerate(self._steppers):
            self._mcu.add_config_cmd(
                "end_stop_set_stepper oid=%d pos=%d stepper_oid=%d" % (
                    self._oid, i, s.get_oid()), is_init=True)
        self._retry_query_ticks = self._mcu.seconds_to_clock(self.RETRY_QUERY)
        self._home_cmd = self._mcu.lookup_command(
            "end_stop_home oid=%c clock=%u rest_ticks=%u pin_value=%c")
        self._query_cmd = self._mcu.lookup_command("end_stop_query oid=%c")
        self._mcu.register_msg(self._handle_end_stop_state, "end_stop_state"
                               , self._oid)
    def home_start(self, print_time, rest_time):
        clock = self._mcu.print_time_to_clock(print_time)
        rest_ticks = self._mcu.seconds_to_clock(rest_time)
        self._homing = True
        self._min_query_time = self._mcu.monotonic()
        self._next_query_clock = clock + self._retry_query_ticks
        msg = self._home_cmd.encode(
            self._oid, clock, rest_ticks, 1 ^ self._invert)
        self._mcu.send(msg, reqclock=clock, cq=self._cmd_queue)
        for s in self._steppers:
            s.note_homing_start(clock)
    def home_finalize(self, print_time):
        for s in self._steppers:
            s.note_homing_finalized()
        self._home_timeout_clock = self._mcu.print_time_to_clock(print_time)
    def home_wait(self):
        eventtime = self._mcu.monotonic()
        while self._check_busy(eventtime):
            eventtime = self._mcu.pause(eventtime + 0.1)
    def _handle_end_stop_state(self, params):
        logging.debug("end_stop_state %s" % (params,))
        self._last_state = params
    def _check_busy(self, eventtime):
        # Check if need to send an end_stop_query command
        if self._mcu.is_fileoutput():
            return False
        last_sent_time = self._last_state.get('#sent_time', -1.)
        if last_sent_time >= self._min_query_time:
            if not self._homing:
                return False
            if not self._last_state.get('homing', 0):
                for s in self._steppers:
                    s.note_homing_triggered()
                self._homing = False
                return False
            last_clock, last_clock_time = self._mcu.get_last_clock()
            if last_clock > self._home_timeout_clock:
                # Timeout - disable endstop checking
                msg = self._home_cmd.encode(self._oid, 0, 0, 0)
                self._mcu.send(msg, reqclock=0, cq=self._cmd_queue)
                raise error("Timeout during endstop homing")
        if self._mcu.is_shutdown:
            raise error("MCU is shutdown")
        last_clock, last_clock_time = self._mcu.get_last_clock()
        if last_clock >= self._next_query_clock:
            self._next_query_clock = last_clock + self._retry_query_ticks
            msg = self._query_cmd.encode(self._oid)
            self._mcu.send(msg, cq=self._cmd_queue)
        return True
    def query_endstop(self, print_time):
        self._homing = False
        self._next_query_clock = self._mcu.print_time_to_clock(print_time)
        self._min_query_time = self._mcu.monotonic()
    def query_endstop_wait(self):
        eventtime = self._mcu.monotonic()
        while self._check_busy(eventtime):
            eventtime = self._mcu.pause(eventtime + 0.1)
        return self._last_state.get('pin', self._invert) ^ self._invert

class MCU_digital_out:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._oid = None
        self._static_value = None
        self._pin = pin_params['pin']
        self._invert = pin_params['invert']
        self._max_duration = 2.
        self._last_clock = 0
        self._last_value = None
        self._cmd_queue = mcu.alloc_command_queue()
        self._set_cmd = None
    def get_mcu(self):
        return self._mcu
    def setup_max_duration(self, max_duration):
        self._max_duration = max_duration
    def setup_static(self):
        self._static_value = not self._invert
    def build_config(self):
        if self._static_value is not None:
            self._mcu.add_config_cmd("set_digital_out pin=%s value=%d" % (
                self._pin, self._static_value))
            return
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd(
            "config_digital_out oid=%d pin=%s default_value=%d"
            " max_duration=%d" % (
                self._oid, self._pin, self._invert,
                self._mcu.seconds_to_clock(self._max_duration)))
        self._set_cmd = self._mcu.lookup_command(
            "schedule_digital_out oid=%c clock=%u value=%c")
    def set_digital(self, print_time, value):
        clock = self._mcu.print_time_to_clock(print_time)
        msg = self._set_cmd.encode(
            self._oid, clock, not not (value ^ self._invert))
        self._mcu.send(msg, minclock=self._last_clock, reqclock=clock
                      , cq=self._cmd_queue)
        self._last_clock = clock
        self._last_value = value
    def get_last_setting(self):
        return self._last_value
    def set_pwm(self, print_time, value):
        self.set_digital(print_time, value >= 0.5)

class MCU_pwm:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._hard_pwm = False
        self._cycle_time = 0.100
        self._max_duration = 2.
        self._oid = None
        self._static_value = None
        self._pin = pin_params['pin']
        self._invert = pin_params['invert']
        self._last_clock = 0
        self._pwm_max = 0.
        self._cmd_queue = mcu.alloc_command_queue()
        self._set_cmd = None
    def get_mcu(self):
        return self._mcu
    def setup_max_duration(self, max_duration):
        self._max_duration = max_duration
    def setup_cycle_time(self, cycle_time):
        self._cycle_time = cycle_time
        self._hard_pwm = False
    def setup_hard_pwm(self, hard_cycle_ticks):
        if not hard_cycle_ticks:
            return
        self._cycle_time = hard_cycle_ticks
        self._hard_pwm = True
    def setup_static_pwm(self, value):
        if self._invert:
            value = 1. - value
        self._static_value = max(0., min(1., value))
    def build_config(self):
        if self._hard_pwm:
            self._pwm_max = self._mcu.serial.msgparser.get_constant_float(
                "PWM_MAX")
            if self._static_value is not None:
                value = int(self._static_value * self._pwm_max + 0.5)
                self._mcu.add_config_cmd(
                    "set_pwm_out pin=%s cycle_ticks=%d value=%d" % (
                        self._pin, self._cycle_time, value))
                return
            self._oid = self._mcu.create_oid()
            self._mcu.add_config_cmd(
                "config_pwm_out oid=%d pin=%s cycle_ticks=%d default_value=%d"
                " max_duration=%d" % (
                    self._oid, self._pin, self._cycle_time, self._invert,
                    self._mcu.seconds_to_clock(self._max_duration)))
            self._set_cmd = self._mcu.lookup_command(
                "schedule_pwm_out oid=%c clock=%u value=%hu")
        else:
            self._pwm_max = self._mcu.serial.msgparser.get_constant_float(
                "SOFT_PWM_MAX")
            if self._static_value is not None:
                if self._static_value != 0. and self._static_value != 1.:
                    raise pins.error("static value on soft pwm not supported")
                self._mcu.add_config_cmd("set_digital_out pin=%s value=%d" % (
                    self._pin, self._static_value >= 0.5))
                return
            self._oid = self._mcu.create_oid()
            self._mcu.add_config_cmd(
                "config_soft_pwm_out oid=%d pin=%s cycle_ticks=%d"
                " default_value=%d max_duration=%d" % (
                    self._oid, self._pin,
                    self._mcu.seconds_to_clock(self._cycle_time),
                    self._invert,
                    self._mcu.seconds_to_clock(self._max_duration)))
            self._set_cmd = self._mcu.lookup_command(
                "schedule_soft_pwm_out oid=%c clock=%u value=%hu")
    def set_pwm(self, print_time, value):
        clock = self._mcu.print_time_to_clock(print_time)
        if self._invert:
            value = 1. - value
        value = int(max(0., min(1., value)) * self._pwm_max + 0.5)
        msg = self._set_cmd.encode(self._oid, clock, value)
        self._mcu.send(msg, minclock=self._last_clock, reqclock=clock
                      , cq=self._cmd_queue)
        self._last_clock = clock

class MCU_adc:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._pin = pin_params['pin']
        self._min_sample = self._max_sample = 0.
        self._sample_time = self._report_time = 0.
        self._sample_count = 0
        self._report_clock = 0
        self._oid = self._callback = None
        self._inv_max_adc = 0.
        self._cmd_queue = mcu.alloc_command_queue()
    def get_mcu(self):
        return self._mcu
    def setup_minmax(self, sample_time, sample_count, minval=0., maxval=1.):
        self._sample_time = sample_time
        self._sample_count = sample_count
        self._min_sample = minval
        self._max_sample = maxval
    def setup_adc_callback(self, report_time, callback):
        self._report_time = report_time
        self._callback = callback
    def build_config(self):
        if not self._sample_count:
            return
        self._oid = self._mcu.create_oid()
        self._mcu.add_config_cmd("config_analog_in oid=%d pin=%s" % (
            self._oid, self._pin))
        last_clock, last_clock_time = self._mcu.get_last_clock()
        clock = last_clock + self._mcu.seconds_to_clock(
            1.0 + self._oid * 0.01) # XXX
        sample_ticks = self._mcu.seconds_to_clock(self._sample_time)
        mcu_adc_max = self._mcu.serial.msgparser.get_constant_float("ADC_MAX")
        max_adc = self._sample_count * mcu_adc_max
        self._inv_max_adc = 1.0 / max_adc
        self._report_clock = self._mcu.seconds_to_clock(self._report_time)
        min_sample = max(0, min(0xffff, int(self._min_sample * max_adc)))
        max_sample = max(0, min(0xffff, int(
            math.ceil(self._max_sample * max_adc))))
        self._mcu.add_config_cmd(
            "query_analog_in oid=%d clock=%d sample_ticks=%d sample_count=%d"
            " rest_ticks=%d min_value=%d max_value=%d" % (
                self._oid, clock, sample_ticks, self._sample_count,
                self._report_clock, min_sample, max_sample), is_init=True)
        self._mcu.register_msg(self._handle_analog_in_state, "analog_in_state"
                               , self._oid)
    def _handle_analog_in_state(self, params):
        last_value = params['value'] * self._inv_max_adc
        next_clock = self._mcu.translate_clock(params['next_clock'])
        last_read_clock = next_clock - self._report_clock
        last_read_time = self._mcu.clock_to_print_time(last_read_clock)
        if self._callback is not None:
            self._callback(last_read_time, last_value)

class MCU:
    error = error
    COMM_TIMEOUT = 3.5
    def __init__(self, printer, config, clocksync):
        self._printer = printer
        self._clocksync = clocksync
        # Serial port
        self._serialport = config.get('serial', '/dev/ttyS0')
        if self._serialport.startswith("/dev/rpmsg_"):
            # Beaglbone PRU
            baud = 0
        else:
            baud = config.getint('baud', 250000, minval=2400)
        self.serial = serialhdl.SerialReader(
            printer.reactor, self._serialport, baud)
        self.is_shutdown = False
        self._shutdown_msg = ""
        self._timeout_timer = printer.reactor.register_timer(
            self.timeout_handler)
        self._restart_method = 'command'
        if baud:
            rmethods = {m: m for m in ['arduino', 'command', 'rpi_usb']}
            self._restart_method = config.getchoice(
                'restart_method', rmethods, 'arduino')
        # Config building
        if printer.bglogger is not None:
            printer.bglogger.set_rollover_info("mcu", None)
        pins.get_printer_pins(printer).register_chip("mcu", self)
        self._emergency_stop_cmd = None
        self._reset_cmd = self._config_reset_cmd = None
        self._oid_count = 0
        self._config_objects = []
        self._init_cmds = []
        self._config_cmds = []
        self._config_crc = None
        self._pin_map = config.get('pin_map', None)
        self._custom = config.get('custom', '')
        self._mcu_freq = 0.
        # Move command queuing
        ffi_main, self._ffi_lib = chelper.get_ffi()
        self._max_stepper_error = config.getfloat(
            'max_stepper_error', 0.000025, minval=0.)
        self._stepqueues = []
        self._steppersync = None
        # Stats
        self._stats_sumsq_base = 0.
        self._mcu_tick_avg = 0.
        self._mcu_tick_stddev = 0.
        self._mcu_tick_awake = 0.
    def handle_mcu_stats(self, params):
        count = params['count']
        tick_sum = params['sum']
        c = 1.0 / (count * self._mcu_freq)
        self._mcu_tick_avg = tick_sum * c
        tick_sumsq = params['sumsq'] * self._stats_sumsq_base
        self._mcu_tick_stddev = c * math.sqrt(count*tick_sumsq - tick_sum**2)
        self._mcu_tick_awake = tick_sum / self._mcu_freq
    def handle_shutdown(self, params):
        if self.is_shutdown:
            return
        self.is_shutdown = True
        self._shutdown_msg = msg = params['#msg']
        logging.info("%s: %s" % (params['#name'], self._shutdown_msg))
        self.serial.dump_debug()
        prefix = "MCU shutdown: "
        if params['#name'] == 'is_shutdown':
            prefix = "Previous MCU shutdown: "
        self._printer.note_shutdown(prefix + msg + error_help(msg))
    # Connection phase
    def _check_restart(self, reason):
        start_reason = self._printer.get_start_args().get("start_reason")
        if start_reason == 'firmware_restart':
            return
        logging.info("Attempting automated firmware restart: %s" % (reason,))
        self._printer.request_exit('firmware_restart')
        self._printer.reactor.pause(self._printer.reactor.monotonic() + 2.000)
        raise error("Attempt firmware restart failed")
    def connect(self):
        if self.is_fileoutput():
            self._connect_file()
        else:
            if (self._restart_method == 'rpi_usb'
                and not os.path.exists(self._serialport)):
                # Try toggling usb power
                self._check_restart("enable power")
            self.serial.connect()
            self._clocksync.connect(self.serial)
            self._printer.reactor.update_timer(
                self._timeout_timer, self.monotonic() + self.COMM_TIMEOUT)
        self._mcu_freq = self.serial.msgparser.get_constant_float('CLOCK_FREQ')
        self._stats_sumsq_base = self.serial.msgparser.get_constant_float(
            'STATS_SUMSQ_BASE')
        self._emergency_stop_cmd = self.lookup_command("emergency_stop")
        self._reset_cmd = self.try_lookup_command("reset")
        self._config_reset_cmd = self.try_lookup_command("config_reset")
        self.register_msg(self.handle_shutdown, 'shutdown')
        self.register_msg(self.handle_shutdown, 'is_shutdown')
        self.register_msg(self.handle_mcu_stats, 'stats')
        self._build_config()
        self._send_config()
    def _connect_file(self, pace=False):
        # In a debugging mode.  Open debug output file and read data dictionary
        out_fname = self._printer.get_start_args().get('debugoutput')
        outfile = open(out_fname, 'wb')
        dict_fname = self._printer.get_start_args().get('dictionary')
        dfile = open(dict_fname, 'rb')
        dict_data = dfile.read()
        dfile.close()
        self.serial.connect_file(outfile, dict_data)
        self._clocksync.connect_file(self.serial, pace)
        # Handle pacing
        if not pace:
            def dummy_estimated_print_time(eventtime):
                return 0.
            self.estimated_print_time = dummy_estimated_print_time
    def timeout_handler(self, eventtime):
        last_clock, last_clock_time = self.get_last_clock()
        timeout = last_clock_time + self.COMM_TIMEOUT
        if eventtime < timeout:
            return timeout
        logging.info("Timeout with firmware (eventtime=%f last_status=%f)" % (
            eventtime, last_clock_time))
        self._printer.note_mcu_error("Lost communication with firmware")
        return self._printer.reactor.NEVER
    def disconnect(self):
        self.serial.disconnect()
        if self._steppersync is not None:
            self._ffi_lib.steppersync_free(self._steppersync)
            self._steppersync = None
    def stats(self, eventtime):
        msg = "mcu_awake=%.03f mcu_task_avg=%.06f mcu_task_stddev=%.06f" % (
            self._mcu_tick_awake, self._mcu_tick_avg, self._mcu_tick_stddev)
        return ' '.join([self.serial.stats(eventtime),
                         self._clocksync.stats(eventtime), msg])
    def force_shutdown(self):
        self.send(self._emergency_stop_cmd.encode())
    def microcontroller_restart(self):
        reactor = self._printer.reactor
        if self._restart_method == 'rpi_usb':
            logging.info("Attempting a microcontroller reset via rpi usb power")
            self.disconnect()
            chelper.run_hub_ctrl(0)
            reactor.pause(reactor.monotonic() + 2.000)
            chelper.run_hub_ctrl(1)
            return
        if self._restart_method == 'command':
            last_clock, last_clock_time = self.get_last_clock()
            eventtime = reactor.monotonic()
            if ((self._reset_cmd is None and self._config_reset_cmd is None)
                or eventtime > last_clock_time + self.COMM_TIMEOUT):
                logging.info("Unable to issue reset command")
                return
            if self._reset_cmd is None:
                # Attempt reset via config_reset command
                logging.info("Attempting a microcontroller config_reset command")
                self.is_shutdown = True
                self.force_shutdown()
                reactor.pause(reactor.monotonic() + 0.015)
                self.send(self._config_reset_cmd.encode())
                reactor.pause(reactor.monotonic() + 0.015)
                self.disconnect()
                return
            # Attempt reset via reset command
            logging.info("Attempting a microcontroller reset command")
            self.send(self._reset_cmd.encode())
            reactor.pause(reactor.monotonic() + 0.015)
            self.disconnect()
            return
        # Attempt reset via arduino mechanism
        logging.info("Attempting a microcontroller reset")
        self.disconnect()
        serialhdl.arduino_reset(self._serialport, reactor)
    def is_fileoutput(self):
        return self._printer.get_start_args().get('debugoutput') is not None
    # Configuration phase
    def _add_custom(self):
        for line in self._custom.split('\n'):
            line = line.strip()
            cpos = line.find('#')
            if cpos >= 0:
                line = line[:cpos].strip()
            if not line:
                continue
            self.add_config_cmd(line)
    def _build_config(self):
        # Build config commands
        for co in self._config_objects:
            co.build_config()
        self._add_custom()
        self._config_cmds.insert(0, "allocate_oids count=%d" % (
            self._oid_count,))

        # Resolve pin names
        mcu = self.serial.msgparser.get_constant('MCU')
        pnames = pins.get_pin_map(mcu, self._pin_map)
        updated_cmds = []
        for cmd in self._config_cmds:
            try:
                updated_cmds.append(pins.update_command(cmd, pnames))
            except:
                raise pins.error("Unable to translate pin name: %s" % (cmd,))
        self._config_cmds = updated_cmds

        # Calculate config CRC
        self._config_crc = zlib.crc32('\n'.join(self._config_cmds)) & 0xffffffff
        self.add_config_cmd("finalize_config crc=%d" % (self._config_crc,))
    def _send_config(self):
        msg = self.create_command("get_config")
        if self.is_fileoutput():
            config_params = {
                'is_config': 0, 'move_count': 500, 'crc': self._config_crc}
        else:
            config_params = self.serial.send_with_response(msg, 'config')
        if not config_params['is_config']:
            if self._restart_method == 'rpi_usb':
                # Only configure mcu after usb power reset
                self._check_restart("full reset before config")
            # Send config commands
            logging.info("Sending printer configuration...")
            for c in self._config_cmds:
                self.send(self.create_command(c))
            if not self.is_fileoutput():
                config_params = self.serial.send_with_response(msg, 'config')
                if not config_params['is_config']:
                    if self.is_shutdown:
                        raise error("Firmware error during config: %s" % (
                            self._shutdown_msg,))
                    raise error("Unable to configure printer")
        else:
            start_reason = self._printer.get_start_args().get("start_reason")
            if start_reason == 'firmware_restart':
                raise error("Failed automated reset of micro-controller")
        if self._config_crc != config_params['crc']:
            self._check_restart("CRC mismatch")
            raise error("Printer CRC does not match config")
        move_count = config_params['move_count']
        logging.info("Configured (%d moves)" % (move_count,))
        if self._printer.bglogger is not None:
            msgparser = self.serial.msgparser
            info = [
                "Configured (%d moves)" % (move_count,),
                "Loaded %d commands (%s)" % (
                    len(msgparser.messages_by_id), msgparser.version),
                "MCU config: %s" % (" ".join(
                    ["%s=%s" % (k, v) for k, v in msgparser.config.items()]))]
            self._printer.bglogger.set_rollover_info("mcu", "\n".join(info))
        self._steppersync = self._ffi_lib.steppersync_alloc(
            self.serial.serialqueue, self._stepqueues, len(self._stepqueues),
            move_count)
        for c in self._init_cmds:
            self.send(self.create_command(c))
    # Config creation helpers
    def setup_pin(self, pin_params):
        pcs = {'stepper': MCU_stepper, 'endstop': MCU_endstop,
               'digital_out': MCU_digital_out, 'pwm': MCU_pwm, 'adc': MCU_adc}
        pin_type = pin_params['type']
        if pin_type not in pcs:
            raise pins.error("pin type %s not supported on mcu" % (pin_type,))
        co = pcs[pin_type](self, pin_params)
        self.add_config_object(co)
        return co
    def create_oid(self):
        self._oid_count += 1
        return self._oid_count - 1
    def add_config_object(self, co):
        self._config_objects.append(co)
    def add_config_cmd(self, cmd, is_init=False):
        if is_init:
            self._init_cmds.append(cmd)
        else:
            self._config_cmds.append(cmd)
    def register_msg(self, cb, msg, oid=None):
        self.serial.register_callback(cb, msg, oid)
    def register_stepqueue(self, stepqueue):
        self._stepqueues.append(stepqueue)
    def alloc_command_queue(self):
        return self.serial.alloc_command_queue()
    def lookup_command(self, msgformat):
        return self.serial.msgparser.lookup_command(msgformat)
    def try_lookup_command(self, msgformat):
        try:
            return self.serial.msgparser.lookup_command(msgformat)
        except self.serial.msgparser.error as e:
            return None
    def create_command(self, msg):
        return self.serial.msgparser.create_command(msg)
    # Clock syncing
    def print_time_to_clock(self, print_time):
        return int(print_time * self._mcu_freq)
    def clock_to_print_time(self, clock):
        return clock / self._mcu_freq
    def estimated_print_time(self, eventtime):
        return self.clock_to_print_time(self._clocksync.get_clock(eventtime))
    def get_mcu_freq(self):
        return self._mcu_freq
    def seconds_to_clock(self, time):
        return int(time * self._mcu_freq)
    def get_last_clock(self):
        return self._clocksync.get_last_clock()
    def translate_clock(self, clock):
        return self._clocksync.translate_clock(clock)
    def get_max_stepper_error(self):
        return self._max_stepper_error
    # Move command queuing
    def send(self, cmd, minclock=0, reqclock=0, cq=None):
        self.serial.send(cmd, minclock, reqclock, cq=cq)
    def flush_moves(self, print_time):
        if self._steppersync is None:
            return
        clock = self.print_time_to_clock(print_time)
        ret = self._ffi_lib.steppersync_flush(self._steppersync, clock)
        if ret:
            raise error("Internal error in stepcompress")
    def pause(self, waketime):
        return self._printer.reactor.pause(waketime)
    def monotonic(self):
        return self._printer.reactor.monotonic()
    def __del__(self):
        self.disconnect()

Common_MCU_errors = {
    ("Timer too close", "No next step", "Missed scheduling of next "): """
This is generally indicative of an intermittent
communication failure between micro-controller and host.""",
    ("ADC out of range",): """
This generally occurs when a heater temperature exceeds
its configured min_temp or max_temp.""",
    ("Rescheduled timer in the past", "Stepper too far in past"): """
This generally occurs when the micro-controller has been
requested to step at a rate higher than it is capable of
obtaining.""",
    ("Command request",): """
This generally occurs in response to an M112 G-Code command
or in response to an internal error in the host software.""",
}

def error_help(msg):
    for prefixes, help_msg in Common_MCU_errors.items():
        for prefix in prefixes:
            if msg.startswith(prefix):
                return help_msg
    return ""

def add_printer_objects(printer, config):
    mainsync = clocksync.ClockSync(printer.reactor)
    printer.add_object('mcu', MCU(printer, config.getsection('mcu'), mainsync))
