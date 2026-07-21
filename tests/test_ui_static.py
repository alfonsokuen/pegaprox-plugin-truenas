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
