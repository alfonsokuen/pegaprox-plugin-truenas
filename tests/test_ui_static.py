# -*- coding: utf-8 -*-
"""Static checks on ``src/ui/plugin.html`` — this repo has no JS unit-test
harness, so these are pragmatic source-pattern regression guards for bugs
that were found and fixed by inspection (F2 review round 2), not a
substitute for a real JS test runner. Kept intentionally narrow: each
assertion targets the EXACT bug that was found, not a style preference.
"""

import os

PLUGIN_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src', 'ui', 'plugin.html')


def _read_ui():
    with open(PLUGIN_HTML, encoding='utf-8') as f:
        return f.read()


def test_dataset_confirm_button_is_not_disabled_for_create():
    """Finding #1 (HIGH — broke the feature entirely): the confirm button
    used to be disabled for every op except 'update', permanently blocking
    dataset creation from the UI (the confirmation field is hidden for
    create, so nothing ever re-enabled it). Guards against the exact
    regression: the button's initial disabled state must depend on
    op === 'delete', never op !== 'update'."""
    html = _read_ui()
    assert "disabled = (op !== 'update')" not in html
    assert "disabled = (op === 'delete')" in html


def test_dataset_and_snapshot_buttons_have_a_double_submit_guard():
    """Finding #5: preview/confirm buttons must disable themselves while a
    request is in flight and re-enable in a .finally()."""
    html = _read_ui()
    assert html.count('btn.disabled = true;') >= 4  # 2 dataset + 2 snapshot buttons
    assert html.count('btn.disabled = false; });') >= 4


def test_parse_json_field_never_silently_falls_back_to_empty_object():
    """Finding #6: malformed JSON in the dataset write form used to
    degrade to {} with no error shown. The fixed parseJsonField must
    return an {ok:false, error} shape instead of a bare fallback value."""
    html = _read_ui()
    assert 'function parseJsonField(raw, fallback)' not in html
    assert "{ ok: false, error: e.message }" in html


def test_settings_form_exposes_rw_key_and_tls_server_name():
    """Live feedback (2026-07-20): the backend (config_store.py) has always
    round-tripped api_key_rw and tls_server_name with the same MASK-aware
    logic as api_key_ro, but the Settings form never exposed input fields
    for either — an operator had no way to enable writes or set a real
    TLS SNI name except by hand-editing config.json on the host directly.
    formToInstance() must never hardcode api_key_rw to null again."""
    html = _read_ui()
    assert "api_key_rw: null," not in html
    assert "id='f-key-rw'" in html or 'id="f-key-rw"' in html
    assert "id='f-tls-name'" in html or 'id="f-tls-name"' in html
    assert "document.getElementById('f-key-rw').value" in html
    assert "document.getElementById('f-tls-name').value" in html


def test_pools_tab_renders_a_status_card_grid_not_a_plain_table():
    """Live feedback (2026-07-20): the Pools & Discos tab was a bare
    3-column table; the operator wanted a per-pool status grid matching
    TrueNAS's own native dashboard (name header + Pool Status/Used Space/
    Disks with Errors/Last Scrub rows with check/warning icons). Verifies
    the helpers this relies on are present: poolDiskSummary() walks ALL
    topology vdev groups (not just 'data') for leaf-disk error stats,
    since a faulted disk can sit in cache/log/spare/special vdevs too."""
    html = _read_ui()
    assert 'function poolDiskSummary(pool)' in html
    assert "['data', 'cache', 'dedup', 'log', 'spare', 'special']" in html
    assert 'function formatBytes(n)' in html
    assert "class=\"pool-grid\"" in html
    assert "poolRow(!!p.healthy, 'Pool Status'" in html
    assert "poolRow(disks.errored === 0, 'Disks with Errors'" in html


def test_overview_is_the_default_active_tab_not_settings():
    """Live feedback (2026-07-20): the plugin always opened on Settings —
    dating back to F0 when Settings was the only tab with anything to show.
    Now that real instances exist, Overview must be the default tab, both
    for the nav button and its section, and Settings must not be."""
    html = _read_ui()
    assert '<button data-tab="overview" class="active">' in html
    assert '<button data-tab="settings" class="active">' not in html
    assert '<section class="tab active" id="tab-overview">' in html
    assert '<section class="tab active" id="tab-settings">' not in html


def test_load_config_syncs_selected_instance_after_auto_select():
    """Live bug (2026-07-20, real .64 in production): building <option>
    elements in renderSelector() never fires 'change' — the browser
    auto-picks the first instance once any exist, but state.selectedInstance
    (only ever set by the 'change' listener) stayed '', so every tab showed
    "Elegí una instancia arriba" even with an instance visibly selected in
    the dropdown. loadConfig() must sync state.selectedInstance from the
    select element's actual value right after rendering it."""
    html = _read_ui()
    load_config = html.split('function loadConfig()')[1].split('function saveInstances')[0]
    assert "document.getElementById('instance-select')" in load_config
    assert 'select.value !== state.selectedInstance' in load_config
    assert 'state.selectedInstance = select.value' in load_config


def test_fleet_tab_exists_and_is_wired_cross_instance():
    """F3 (2026-07-20): Fleet is a NEW tab that shows ALL configured
    instances at once — it must never gate on state.selectedInstance the
    way every other tab does, and must call the fleet route with no
    instance_id query param."""
    html = _read_ui()
    assert 'data-tab="fleet"' in html
    assert 'id="tab-fleet"' in html
    assert "fleet: 'fleet-body'" in html
    assert "if (tab === 'fleet')" in html
    assert "api('fleet')" in html
    assert 'function renderFleet(body, payload)' in html


def test_fleet_tab_is_never_cached_client_side():
    """Server already TTL-caches the /fleet route (15s) — the client must
    still refetch on every tab click rather than freezing on a stale
    snapshot, mirroring Overview/Pools' NEVER_CACHE_TABS treatment."""
    html = _read_ui()
    assert "var NEVER_CACHE_TABS = { fleet: true," in html


def test_services_tab_exists_with_control_buttons_wired():
    """F4b (2026-07-20): the Services tab must render Iniciar/Detener/
    Reiniciar buttons per row and route through the same writesDryRun/
    writesExecute flow as datasets/snapshots — not a bare read-only table."""
    html = _read_ui()
    assert 'data-tab="services"' in html
    assert 'id="tab-services"' in html
    assert "services: 'services-body'" in html
    assert 'function renderServices(body, items)' in html
    assert "function openServiceForm(op, serviceName)" in html
    assert "writesDryRun('services', serviceWrite.op" in html
    assert "writesExecute(state.selectedInstance, 'services', serviceWrite.op" in html


def test_services_tab_only_offers_valid_actions_per_current_state():
    """A running service must offer stop/restart, not start (and vice
    versa) — guards against a button that would just re-confirm the
    service is already in that state."""
    html = _read_ui()
    assert "var ops = running ? ['stop', 'restart'] : ['start'];" in html


def test_apps_vms_tab_has_lifecycle_action_buttons_wired():
    """F5 (2026-07-20): the Apps/VMs tab must offer start/stop/restart
    (VMs) and start/stop/redeploy (apps) via the same writesDryRun/
    writesExecute flow as services/datasets — not a bare read-only table."""
    html = _read_ui()
    assert 'function renderAppsVms(body, data)' in html
    assert 'function openResourceForm(subsystem, op, id, opLabel)' in html
    assert "writesDryRun(resourceWrite.subsystem, resourceWrite.op" in html
    assert "writesExecute(state.selectedInstance, resourceWrite.subsystem, resourceWrite.op" in html


def test_apps_never_offer_a_restart_op_only_redeploy():
    """Apps have no 'restart' method on TrueNAS-25.10.1 (only 'redeploy',
    which also pulls new images) — the UI must never offer a 'restart' op
    for apps, only for VMs."""
    html = _read_ui()
    assert "var APP_OP_LABEL = { start: 'Iniciar', stop: 'Detener', redeploy: 'Redeploy' };" in html
    assert "(running ? ['stop', 'redeploy'] : ['start'])" in html
    assert "(running ? ['stop', 'restart'] : ['start'])" in html


def test_data_protection_tab_exists_and_is_wired():
    """F6 (2026-07-20): cloudsync/rsync/certificate posture tab."""
    html = _read_ui()
    assert 'data-tab="data-protection"' in html
    assert 'id="tab-data-protection"' in html
    assert "'data-protection': 'data-protection-body'" in html
    assert "'data-protection': 'data_protection'" in html
    assert 'function renderDataProtection(body, data)' in html


def test_data_protection_tab_never_renders_raw_credentials_or_keys():
    """The backend already strips secrets (see data_protection.py), but the
    UI must also never reference the raw fields as a defense-in-depth
    regression guard — it should only read the projected fields."""
    html = _read_ui()
    section = html.split('function renderDataProtection(body, data)')[1].split('function renderSimpleTable')[0]
    for forbidden in ['.credentials', '.privatekey', '.certificate)', 't.CSR', 'chain_list']:
        assert forbidden not in section


def test_overview_fetches_and_renders_telemetry_sparklines():
    """Charts (2026-07-20): the CPU/Memory/Network graphs requested after
    the native-TrueNAS-dashboard screenshot, explicitly deferred until
    after the Storage grid ('gráficos después') — now built."""
    html = _read_ui()
    assert "fetchSubsystem('telemetry')" in html
    assert 'function renderTelemetryCards(telemetry)' in html
    assert 'function renderOverview(body, sysRes, poolsRes, telemetryRes)' in html
    assert 'function renderSparkline(series, opts)' in html
    assert 'function renderDualSparkline(series, opts)' in html


def test_memory_sparkline_is_clamped_0_to_100_percent_not_auto_scaled():
    """Memory is a used-percentage (0-100) — auto-scaling its min/max like
    a generic sparkline would exaggerate small fluctuations into a
    misleading full-height swing."""
    html = _read_ui()
    assert "renderSparkline(telemetry.memory, { min: 0, max: 100" in html
