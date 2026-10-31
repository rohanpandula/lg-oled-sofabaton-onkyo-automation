"""Validate the published packages in a disposable Home Assistant config directory."""
import asyncio
from pathlib import Path
import shutil
import tempfile

import yaml
from homeassistant import loader
from homeassistant.config import async_check_ha_config_file
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

ROOT = Path(__file__).resolve().parents[1]


async def main():
    with tempfile.TemporaryDirectory(prefix='av-example-config-') as folder:
        config = Path(folder)
        shutil.copytree(ROOT / 'packages', config / 'packages')
        for name in ['baby_cry_alert.yaml', 'onkyo_audio_auto.yaml']:
            shutil.copyfile(ROOT / 'optional' / name, config / 'packages' / name)
        shutil.copyfile(ROOT / 'secrets.yaml.example', config / 'secrets.yaml')
        (config / 'configuration.yaml').write_text(
            'homeassistant:\n  packages: !include_dir_named packages\nwake_on_lan:\n')
        hass = HomeAssistant(str(config))
        loader.async_setup(hass)
        try:
            error = await async_check_ha_config_file(hass)
            assert error is None, error
        finally:
            await hass.async_stop(force=True)

    # These names are referenced by other examples; fresh-install slugs must match.
    music = yaml.safe_load((ROOT / 'packages/music_mode.yaml').read_text())
    assert slugify(music['script']['lg_tv_music_mode']['alias']) == 'lg_tv_music_mode'
    # BaseLoader treats !secret as ordinary strings for this naming-only check.
    core = yaml.load((ROOT / 'packages/av_coordination.yaml').read_text(), Loader=yaml.BaseLoader)
    aliases = {a['id']: slugify(a['alias']) for a in core['automation']}
    assert aliases['lg_tv_wake_for_apple_tv'] == 'lg_tv_wake_for_apple_tv'
    assert aliases['sofabaton_auto_start_appletv_on_cec'] == 'sofabaton_auto_start_watch_appletv_on_cec_session'
    print('PASS: package configuration and referenced fresh-install entity names')


if __name__ == '__main__':
    asyncio.run(main())
