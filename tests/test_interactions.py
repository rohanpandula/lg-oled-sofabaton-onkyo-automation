"""Run repository examples through native HA scripts with isolated state and fake services.

No live configuration is read and no commands reach real devices.
"""
import asyncio
import contextvars
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile

import yaml
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script
from homeassistant.util import dt as dt_util

TV = 'binary_sensor.lg_tv_power'
PANEL = 'switch.lg_tv_display_panel'
ATV = 'media_player.apple_tv'
AVR = 'media_player.onkyo'
CORE = 'media_player.coreelec'
HUB = 'select.x1s_hub_activity'
APP = 'sensor.lg_tv_active_app'
WAKER = 'automation.lg_tv_wake_for_apple_tv'


ROOT = Path(__file__).resolve().parents[1]


class ExampleLoader(yaml.SafeLoader):
    """Resolve only the documented example secrets, without reading secrets.yaml."""


ExampleLoader.add_constructor('!secret', lambda loader, node: {
    'lg_tv_mac': '02:00:00:00:00:01',
    'lan_broadcast': '192.0.2.255',
}[loader.construct_scalar(node)])


def load_config():
    core = yaml.load((ROOT / 'packages/av_coordination.yaml').read_text(), Loader=ExampleLoader)
    cry = yaml.safe_load((ROOT / 'optional/baby_cry_alert.yaml').read_text())
    audio = yaml.safe_load((ROOT / 'optional/onkyo_audio_auto.yaml').read_text())
    return {'automations': core['automation'] + cry['automation'] + audio['automation'],
            'music': yaml.safe_load((ROOT / 'packages/music_mode.yaml').read_text())}


class Isolated:
    def __init__(self, config):
        self.directory = tempfile.TemporaryDirectory(prefix='av-interaction-audit-')
        self.hass = HomeAssistant(self.directory.name)
        self.config = config
        self.automations = {a['id']: a for a in config['automations']}
        self.calls = []
        self.scripts = []
        self.background = []
        self.sync_started = asyncio.Event()
        for entity, value in [(TV, 'on'), (PANEL, 'on'), (ATV, 'playing'),
                              (AVR, 'on'), (CORE, 'off'), (HUB, 'Powered Off')]:
            self.set(entity, value)
        self.set(AVR, 'on', source='STRM BOX')
        self.set(APP, 'AV Receiver', app_id='com.webos.app.hdmi4')
        self.controller = None
        shared = config['music'].get('script', {}).get('lg_tv_music_mode')
        if shared:
            self.controller = self.make(shared['sequence'], 'music controller', shared['mode'])
        for domain, service in [('switch', 'turn_on'), ('switch', 'turn_off'),
                                ('automation', 'trigger'), ('button', 'press'),
                                ('kodi', 'call_method'), ('select', 'select_option'),
                                ('media_player', 'turn_on'), ('media_player', 'turn_off'),
                                ('media_player', 'select_sound_mode'),
                                ('text', 'set_value'), ('wake_on_lan', 'send_magic_packet'),
                                ('script', 'lg_tv_music_mode'), ('script', 'turn_on')]:
            self.hass.services.async_register(domain, service, self.service)

    def set(self, entity, value, **attrs):
        self.hass.states.async_set(entity, value, attrs)

    async def service(self, call):
        data = dict(call.data)
        self.calls.append((call.domain, call.service, data))
        entities = data.get('entity_id', [])
        entities = [entities] if isinstance(entities, str) else entities
        if call.domain == 'automation':
            self.sync_started.set()
        elif call.domain == 'switch' and PANEL in entities:
            self.set(PANEL, 'on' if call.service == 'turn_on' else 'off')
        elif call.domain == 'script':
            assert self.controller is not None
            if call.service == 'turn_on':
                self.background.append(asyncio.create_task(
                    self.controller.async_run(data['variables'], context=Context()),
                    context=contextvars.Context()))
            else:
                await self.controller.async_run(data, context=Context())

    def make(self, sequence, name='audit', mode='single'):
        sequence = deepcopy(sequence)
        # Keep tests quick; all conditions, waits, ordering and cancellation are native.
        def shorten(items):
            for step in items:
                if 'delay' in step:
                    step['delay'] = {'milliseconds': 20}
                if 'timeout' in step:
                    step['timeout'] = {'milliseconds': 700}
                for choice in step.get('choose', []):
                    shorten(choice.get('sequence', []))
                shorten(step.get('default', []))
        shorten(sequence)
        script = Script(self.hass, cv.SCRIPT_SCHEMA(sequence), name, 'audit', script_mode=mode)
        self.scripts.append(script)
        return script

    async def automation(self, identity, trigger=None):
        a = self.automations[identity]
        script = self.make(a.get('conditions', []) + a['actions'], identity, a.get('mode', 'single'))
        await script.async_run({'trigger': trigger or {'id': 'periodic'}}, context=Context())

    def count(self, domain, service):
        return sum(d == domain and s == service for d, s, _ in self.calls)

    async def close(self):
        for script in self.scripts:
            await script.async_stop()
        await self.flush()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def flush(self):
        if self.background:
            await asyncio.wait_for(asyncio.gather(*self.background, return_exceptions=True), 2)


async def music_cancel(config, cry=False):
    test = Isolated(config)
    on_task = None
    try:
        switch = config['music']['template'][0]['switch'][0]
        on = test.make(switch['turn_on'], 'Music On')
        off = test.make(switch['turn_off'], 'Music Off')
        on_task = asyncio.create_task(on.async_run(context=Context()))
        await asyncio.wait_for(test.sync_started.wait(), 2)
        if cry:
            await test.automation('lg_tv_baby_cry_alert')
        else:
            await off.async_run(context=Context())
        # Complete the physical hub startup after the later Off/cry request.
        test.set(HUB, 'Watch AppleTV')
        await asyncio.wait_for(asyncio.gather(on_task, return_exceptions=True), 2)
        await test.flush()
        await asyncio.sleep(.03)
        assert test.hass.states.get(PANEL).state == 'on', 'old Music On blanked the panel after a newer Off/cry request'
        if cry:
            assert test.count('text', 'set_value') == 1
    finally:
        if on_task and not on_task.done():
            on_task.cancel()
            await asyncio.gather(on_task, return_exceptions=True)
        await test.close()


async def quick_shutdown(config, activity='Powered Off', receiver='off', app='com.webos.app.hdmi4'):
    test = Isolated(config)
    try:
        test.set(HUB, activity)
        test.set(ATV, 'off')
        test.set(AVR, receiver, source='NETWORK' if receiver == 'on' else None)
        test.set(APP, app, app_id=app)
        await test.automation('lg_tv_sleep_with_apple_tv')
        expected = activity in ['Watch AppleTV', 'Powered Off'] and receiver == 'off' and app == 'com.webos.app.hdmi4'
        assert bool(test.count('button', 'press')) == expected, 'quick shutdown or other-input exclusion is wrong'
    finally:
        await test.close()


async def core_switch(config, source='DVD ··· BD/DVD'):
    test = Isolated(config)
    try:
        test.set(HUB, 'Watch AppleTV')  # hub report has not caught up with physical input
        test.set(CORE, 'idle')
        test.set(AVR, 'on', source=source)
        await test.automation('ugoos_suspend_off_dolby_vision')
        assert bool(test.count('kodi', 'call_method')) == (source == 'STRM BOX'), 'periodic cleanup interrupted the receiver-selected CoreELEC session'
    finally:
        await test.close()


async def tv_off_cleanup(config, fresh_wake, source='STRM BOX'):
    test = Isolated(config)
    try:
        test.set(HUB, 'Watch AppleTV')
        test.set(AVR, 'on', source=source)
        test.set(TV, 'off')
        off_state = test.hass.states.get(TV)
        when = off_state.last_changed + timedelta(seconds=1 if fresh_wake else -1)
        test.set(WAKER, 'on', last_triggered=when)
        await test.automation('sofabaton_lg_tv_off_sync', {'id': '0', 'to_state': off_state})
        expected = not fresh_wake and source != 'NETWORK'
        assert bool(test.count('select', 'select_option')) == expected, 'TV-off cleanup ended a new wake or receiver-only AirPlay'
    finally:
        await test.close()


async def restored_hub(config, previous):
    test = Isolated(config)
    try:
        a = test.automations['atv_wake_on_appletv_activity']
        trigger = a['triggers'][0]
        allowed = trigger.get('from')
        matches = allowed is None or previous in (allowed if isinstance(allowed, list) else [allowed])
        if matches:
            await test.automation('atv_wake_on_appletv_activity')
        assert bool(test.count('media_player', 'turn_on')) == (previous == 'Powered Off'), 'restored hub state woke Apple TV'
    finally:
        await test.close()


async def delayed_atv_sleep(config, fresh_wake=False, receiver='off'):
    test = Isolated(config)
    try:
        test.set(TV, 'off')
        test.set(AVR, receiver, source='NETWORK' if receiver == 'on' else None)
        hub_state = test.hass.states.get(HUB)
        if fresh_wake:
            await asyncio.sleep(.002)
            test.set(ATV, 'paused')
        await test.automation('atv_sleep_on_hub_power_off', {'id': '0', 'to_state': hub_state})
        expected = not fresh_wake and receiver == 'off'
        assert bool(test.count('media_player', 'turn_off')) == expected, 'delayed Apple TV sleep ended a new wake or AirPlay'
    finally:
        await test.close()


async def backstop(config, resumed_player):
    test = Isolated(config)
    task = None
    try:
        test.set(TV, 'off')
        test.set(ATV, 'off')
        task = asyncio.create_task(test.automation('sofabaton_tv_off_backstop'))
        await asyncio.sleep(.008)
        test.set(TV, 'on')
        test.set(resumed_player, 'playing')
        await task
        assert test.count('button', 'press') == 0, 'late TV-off backstop interrupted resumed playback'
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await test.close()


async def music_after_sync(config, shut_down=False):
    test = Isolated(config)
    task = None
    try:
        switch = config['music']['template'][0]['switch'][0]
        on = test.make(switch['turn_on'], 'Music On')
        task = asyncio.create_task(on.async_run(context=Context()))
        await asyncio.wait_for(test.sync_started.wait(), 2)
        if shut_down:
            test.set(TV, 'off')
        test.set(HUB, 'Watch AppleTV')
        await asyncio.wait_for(task, 2)
        await test.flush()
        blanks = [c for c in test.calls if c[0:2] == ('switch', 'turn_off')]
        assert bool(blanks) != shut_down, 'pending Music On sent a blank command after TV shutdown'
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await test.close()


async def audio_auto(config, cancel=None):
    test = Isolated(config)
    task = None
    try:
        a = test.automations['onkyo_native_audio_auto']
        script = test.make(a['actions'], 'native audio Auto', a['mode'])
        test.set('input_boolean.onkyo_audio_auto', 'on')
        test.set('sensor.onkyo_auto_target', 'Dolby Atmos / Surround')
        test.set(AVR, 'on', source='STRM BOX', sound_mode='Direct',
                 sound_mode_list=['Direct', 'Dolby Atmos / Surround'])
        task = asyncio.create_task(script.async_run(context=Context()))
        await asyncio.sleep(.008)
        if cancel == 'manual':
            test.set('input_boolean.onkyo_audio_auto', 'off')
            await script.async_run(context=Context())
        elif cancel == 'standby':
            test.set(AVR, 'off')
            await script.async_run(context=Context())
        await task
        assert bool(test.count('media_player', 'select_sound_mode')) == (cancel is None), 'audio Auto overrode manual mode or acted after standby'
        assert test.count('media_player', 'turn_on') == 0 and test.count('media_player', 'turn_off') == 0
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await test.close()


async def receiver_source_wake(config, previous):
    test = Isolated(config)
    try:
        test.set(TV, 'off')
        test.set(AVR, 'on', source=previous)
        old = test.hass.states.get(AVR)
        test.set(AVR, 'on', source='STRM BOX')
        a = test.automations['lg_tv_wake_for_apple_tv']
        trigger_id = a['triggers'][2].get('id', '2')
        await test.automation('lg_tv_wake_for_apple_tv', {'id': trigger_id, 'from_state': old})
        assert bool(test.count('wake_on_lan', 'send_magic_packet')) == (previous == 'NETWORK'), 'restored source metadata woke the TV'
    finally:
        await test.close()


async def main(config):
    config = deepcopy(config)
    cases = [
        ('Music Off cancels pending Music On', lambda: music_cancel(config)),
        ('Cry alert cancels pending Music On', lambda: music_cancel(config, cry=True)),
        ('Apple TV off before hub sync', lambda: quick_shutdown(config)),
        ('Apple TV off after hub sync', lambda: quick_shutdown(config, activity='Watch AppleTV')),
        ('CoreELEC activity excluded from TV sleep', lambda: quick_shutdown(config, activity='Watch Dolby Vision')),
        ('AirPlay excluded from TV sleep', lambda: quick_shutdown(config, receiver='on')),
        ('Native TV app excluded from Apple TV sleep', lambda: quick_shutdown(config, activity='Watch AppleTV', app='youtube.leanback.v4')),
        ('CoreELEC selected before hub catches up', lambda: core_switch(config)),
        ('CoreELEC idle cleanup during Apple TV', lambda: core_switch(config, source='STRM BOX')),
        ('Unknown receiver input cannot suspend CoreELEC', lambda: core_switch(config, source=None)),
        ('Fresh wake cancels old TV-off cleanup', lambda: tv_off_cleanup(config, fresh_wake=True)),
        ('Real TV-off still ends hub activity', lambda: tv_off_cleanup(config, fresh_wake=False)),
        ('TV-off preserves receiver-only AirPlay', lambda: tv_off_cleanup(config, fresh_wake=False, source='NETWORK')),
        ('Hub reconnect cannot wake Apple TV', lambda: restored_hub(config, previous='unavailable')),
        ('Real hub activity start still wakes Apple TV', lambda: restored_hub(config, previous='Powered Off')),
        ('Delayed Apple TV sleep respects newer wake', lambda: delayed_atv_sleep(config, fresh_wake=True)),
        ('Delayed Apple TV sleep respects new AirPlay', lambda: delayed_atv_sleep(config, receiver='on')),
        ('Normal hub shutdown still sleeps Apple TV', lambda: delayed_atv_sleep(config)),
        ('Late TV backstop respects Apple TV playback', lambda: backstop(config, ATV)),
        ('Late TV backstop respects CoreELEC playback', lambda: backstop(config, CORE)),
        ('Music On completes after hub sync', lambda: music_after_sync(config)),
        ('Music On aborts if TV shuts down during sync', lambda: music_after_sync(config, shut_down=True)),
        ('Audio Auto selects the current native mode', lambda: audio_auto(config)),
        ('Manual audio override cancels pending Auto', lambda: audio_auto(config, cancel='manual')),
        ('Standby cancels pending audio-mode change', lambda: audio_auto(config, cancel='standby')),
        ('Restored receiver input cannot wake TV', lambda: receiver_source_wake(config, previous=None)),
        ('Unknown receiver input cannot wake TV', lambda: receiver_source_wake(config, previous='unknown')),
        ('Real NETWORK to Apple TV input change wakes TV', lambda: receiver_source_wake(config, previous='NETWORK')),
    ]
    failures = []
    for name, run in cases:
        try:
            await run()
        except Exception as error:
            failures.append(name)
            print('FAIL', name, type(error).__name__, str(error), flush=True)
        else:
            print('PASS', name, flush=True)
    print(json.dumps({'cases': len(cases), 'failures': failures}), flush=True)
    return not failures


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main(load_config())) else 1)
