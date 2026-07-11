let currentApp = null;       // anchor name of the selected app — identity, never display
let pollTimer = null;
let latestLogId = 0;
let autoScroll = true;
let appsCache = [];          // last get_apps() result; used to look up display_name for log header
let dockerMode = false;      // set once at startup by initDockerMode(), never changes mid-session

// Docker mode: fetched once at startup, before the first app-list render.
// When the monitor runs inside the Hearth container (HEARTH_DOCKER env),
// the local-machine surfaces are gated:
//
//   - per-row OPEN buttons — would spawn a window-mode process that dies on
//     Qt import with stderr discarded (the silent click-and-nothing-happens
//     failure mode)
//   - the Launchers panel — writes .desktop files to the container's
//     invisible filesystem; its Tray and Terminal columns are equally dead
//     (tray is local-mode-only, terminal wraps OPEN/launcher spawns)
//   - the settings modal's form — its contents (the monitor's own launcher
//     + tray sections and the uv toggle) are all local-machine concerns,
//     and the uv toggle is actively destructive in the container (system
//     Python has no Flask; no-uv mode would crash every app start)
//
// The corresponding backend endpoints carry matching guards — hiding
// buttons is UX; the guards are correctness, since serve mode is
// LAN-exposed and hidden buttons don't remove endpoints.
async function initDockerMode() {
    try {
        const result = await app.call('get_docker_mode');
        dockerMode = !!(result && result.docker);
    } catch (e) {
        dockerMode = false;  // endpoint missing (older backend) — assume native
    }
    if (dockerMode) {
        // Launchers is wholly inapplicable in the container — hidden rather
        // than explained. The settings cogwheel stays visible and explains
        // itself when opened (see openSettingsDialog), since it's the
        // designated home for any future docker-specific settings.
        document.getElementById('btn-launchers').style.display = 'none';
    }
}

async function refreshApps() {
    appsCache = await app.call('get_apps');
    renderAppList(appsCache);
    if (currentApp) updateLogTitle();
}

let portValues = {};  // anchor name -> port string, survives DOM rebuilds

function renderAppList(apps) {
    const container = document.getElementById('apps-container');

    // Save current port input values before destroying DOM
    container.querySelectorAll('.port-input').forEach(input => {
        const name = input.id.replace('port-', '');
        portValues[name] = input.value;
    });

    // Remember which input had focus
    const focused = document.activeElement;
    const focusedPortId = focused && focused.classList.contains('port-input') ? focused.id : null;

    container.innerHTML = '';

    if (apps.length === 0) {
        container.innerHTML = '<div class="no-apps">No Hearth apps found</div>';
        return;
    }

    apps.forEach(a => {
        const row = document.createElement('div');
        row.className = 'app-row' + (a.name === currentApp ? ' selected' : '');
        row.dataset.name = a.name;  // anchor name — used for selection lookups, never display
        row.onclick = () => selectApp(a.name);
        // Tooltip exposes the anchor name when it differs from what's shown,
        // so an aliased app's underlying identity is always one hover away.
        if (a.alias) row.title = a.name;

        const indicator = document.createElement('span');
        indicator.className = 'status-dot ' + a.status;
        indicator.title = a.status;

        const nameEl = document.createElement('span');
        nameEl.className = 'app-name';
        nameEl.textContent = a.display_name;
        nameEl.ondblclick = (e) => {
            e.stopPropagation();
            e.preventDefault();
            enterRenameMode(nameEl, a);
        };

        const controls = document.createElement('span');
        controls.className = 'app-controls';

        if (a.status === 'running') {
            if (a.port) {
                const portLabel = document.createElement('span');
                portLabel.className = 'port-label';
                portLabel.textContent = ':' + a.port;
                controls.appendChild(portLabel);
            }
            const stopBtn = document.createElement('button');
            stopBtn.className = 'btn-stop';
            stopBtn.textContent = 'Stop';
            stopBtn.title = 'Stop ' + a.name;
            stopBtn.onclick = (e) => { e.stopPropagation(); stopApp(a.name); };
            controls.appendChild(stopBtn);
        } else {
            // Note: the 'exited' status is communicated by the red status
            // dot to the left of the name, so no separate text tag is
            // needed here. Showing both was redundant.
            const portInput = document.createElement('input');
            portInput.type = 'text';
            portInput.className = 'port-input';
            portInput.placeholder = 'port';
            portInput.id = 'port-' + a.name;
            if (portValues[a.name] != null) {
                portInput.value = portValues[a.name];
            } else if (a.saved_port) {
                portInput.value = a.saved_port;
            }
            portInput.onclick = (e) => e.stopPropagation();
            portInput.onkeydown = (e) => {
                if (e.key === 'Enter') { e.stopPropagation(); startApp(a.name); }
            };
            controls.appendChild(portInput);

            const startBtn = document.createElement('button');
            startBtn.className = 'btn-start';
            startBtn.textContent = 'Serve';
            startBtn.title = 'Start ' + a.name + ' in serve mode';
            startBtn.onclick = (e) => { e.stopPropagation(); startApp(a.name); };
            controls.appendChild(startBtn);
        }

        // OPEN button — always present in native mode, regardless of
        // serve-mode state. Launches the app in local mode (its own
        // pywebview window) as a detached process. The framework's
        // single-instance lockfile (local mode only) prevents duplicate
        // launches; the backend short-circuits when an instance is already
        // open and we surface that as a toast.
        // Note: the button stays enabled even when the app is in serve mode.
        // Per design, we don't try to prevent serve+local coexistence in the
        // UI — the user is expected to know what they're doing.
        // In docker mode the button is omitted entirely — there's no host
        // display for a window to open on (see initDockerMode).
        if (!dockerMode) {
            const openBtn = document.createElement('button');
            openBtn.className = 'btn-open';
            openBtn.textContent = 'Open';
            openBtn.title = 'Open ' + a.display_name + ' in a window on this machine';
            openBtn.onclick = (e) => { e.stopPropagation(); openApp(a.name, a.display_name); };
            controls.appendChild(openBtn);
        }

        row.appendChild(indicator);
        row.appendChild(nameEl);
        row.appendChild(controls);
        container.appendChild(row);
    });

    // Restore focus if a port input was active
    if (focusedPortId) {
        const el = document.getElementById(focusedPortId);
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
    }
}

// Replace a name span with an inline text input for editing the alias.
// Enter / blur commits, Escape cancels. Empty or anchor-equal value clears
// the alias. The full apps list is then refreshed to redraw the row.
function enterRenameMode(nameEl, appObj) {
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'rename-input';
    input.value = appObj.display_name;
    input.maxLength = 80;

    let done = false;

    // Stop bubbling so clicks on the input don't re-trigger row selection
    // and double-clicks don't re-enter rename mode.
    input.onclick = (e) => e.stopPropagation();
    input.ondblclick = (e) => e.stopPropagation();

    const commit = async () => {
        if (done) return;
        done = true;
        const newValue = input.value.trim();
        // Only call the API if something actually changed. Comparing against
        // the current display_name covers both "unchanged alias" and
        // "anchor name unchanged with no alias set" without special-casing.
        if (newValue !== appObj.display_name) {
            try {
                await app.call('set_alias', { name: appObj.name, alias: newValue });
            } catch (e) {
                // Silent — refresh will redraw current state regardless
            }
        }
        await refreshApps();
    };

    const cancel = () => {
        if (done) return;
        done = true;
        refreshApps();
    };

    input.onkeydown = (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') {
            e.preventDefault();
            input.blur();  // triggers commit via onblur
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancel();
        }
    };
    input.onblur = commit;

    nameEl.replaceWith(input);
    input.focus();
    input.select();
}

async function startApp(name) {
    const portInput = document.getElementById('port-' + name);
    const port = portInput ? portInput.value.trim() : '';
    const params = { name };
    if (port) params.port = parseInt(port, 10);
    const result = await app.call('start_app', params);
    if (result.error) {
        showLogMessage(name, 'Error: ' + result.error, 'err');
    }
    // Force a full re-select even if this app is already current. The backend
    // discards the old ManagedApp and creates a fresh one whose log IDs
    // restart at 1, so our log cursor and panel contents are stale. Without
    // force, selectApp's early-return optimization would skip the reset and
    // (a) leave polling stopped if it had self-terminated when the previous
    // process exited, and (b) keep latestLogId at the old high-water mark,
    // causing the new process's early lines to be silently filtered out.
    selectApp(name, { force: true });
    refreshApps();
}

async function stopApp(name) {
    const result = await app.call('stop_app', { name });
    if (result.error) {
        showLogMessage(name, 'Error: ' + result.error, 'err');
    }
    refreshApps();
}

function selectApp(name, opts) {
    opts = opts || {};
    // Idempotent — re-selecting the current app would otherwise clear its
    // logs and reset the polling cursor, which is jarring (and matters when
    // the row click that precedes a double-click would otherwise wipe state).
    //
    // Callers that know the app's state is stale despite the name being
    // unchanged (e.g., startApp, where the subprocess has just been replaced
    // with a fresh one whose log IDs restart at 1) pass {force: true} to
    // bypass this optimization and force a full reset.
    if (currentApp === name && !opts.force) {
        return;
    }

    currentApp = name;
    latestLogId = 0;
    currentAppPort = null;
    autoScroll = true;

    // Clear log panel
    const logOutput = document.getElementById('log-output');
    logOutput.innerHTML = '';

    // Update header (uses anchor + alias from cache)
    updateLogTitle();

    // Update actions
    const actions = document.getElementById('log-actions');
    actions.innerHTML = '';
    const clearBtn = document.createElement('button');
    clearBtn.className = 'btn-clear';
    clearBtn.textContent = 'Clear';
    clearBtn.onclick = clearLogs;
    actions.appendChild(clearBtn);

    // Highlight in list — compare against the row's anchor (data-name), since
    // the visible label is the display name and may not equal the anchor.
    document.querySelectorAll('.app-row').forEach(row => {
        row.classList.toggle('selected', row.dataset.name === name);
    });

    // Start polling
    startPolling();
}

// Render the log header from the cached app entry. The anchor name appears
// as a dim suffix when an alias is in effect — the log pane is operator
// territory and the underlying identity is useful when something goes wrong.
function updateLogTitle() {
    const titleEl = document.getElementById('log-title');
    if (!currentApp) {
        titleEl.textContent = 'Select an app';
        return;
    }
    const a = appsCache.find(a => a.name === currentApp);
    if (!a) {
        titleEl.textContent = currentApp;
        return;
    }
    titleEl.innerHTML = '';
    const main = document.createElement('span');
    main.textContent = a.display_name;
    titleEl.appendChild(main);
    if (a.alias) {
        const sub = document.createElement('span');
        sub.className = 'log-title-anchor';
        sub.textContent = ' (' + a.name + ')';
        titleEl.appendChild(sub);
    }
}

function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollLogs(); // immediate first fetch
    pollTimer = setInterval(pollLogs, 1000);
}

let currentAppPort = null;

async function pollLogs() {
    if (!currentApp) return;

    try {
        const data = await app.call('get_logs', { name: currentApp, since: latestLogId });
        if (data.lines && data.lines.length > 0) {
            appendLogLines(data.lines);
            latestLogId = data.latest_id;
        }
        // Port just became available — refresh app list to show it
        if (data.port && data.port !== currentAppPort) {
            currentAppPort = data.port;
            refreshApps();
        }
        // Process exited — update status once, stop polling
        if (!data.running) {
            stopPolling();
            if (data.latest_id > 0) {
                refreshApps();
            }
        }
    } catch (e) {
        // Silently ignore poll errors
    }
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function appendLogLines(lines) {
    const logOutput = document.getElementById('log-output');

    lines.forEach(entry => {
        const line = document.createElement('div');
        line.className = 'log-line ' + entry.stream;
        line.textContent = entry.text;
        logOutput.appendChild(line);
    });

    // Cap displayed lines
    while (logOutput.children.length > MAX_DISPLAY_LINES) {
        logOutput.removeChild(logOutput.firstChild);
    }

    if (autoScroll) {
        logOutput.scrollTop = logOutput.scrollHeight;
    }
}

const MAX_DISPLAY_LINES = 500;

function showLogMessage(name, text, stream) {
    if (currentApp === name) {
        appendLogLines([{ id: 0, stream: stream || 'out', text: text }]);
    }
}

function clearLogs() {
    document.getElementById('log-output').innerHTML = '';
}

// Detect manual scroll to pause auto-scroll
document.addEventListener('DOMContentLoaded', () => {
    const logOutput = document.getElementById('log-output');
    logOutput.addEventListener('scroll', () => {
        const atBottom = logOutput.scrollHeight - logOutput.scrollTop - logOutput.clientHeight < 30;
        autoScroll = atBottom;
    });

    // Esc closes whichever modal is open. Listening at document level because
    // the inputs may swallow keydown events depending on focus.
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        const pwModal = document.getElementById('password-modal');
        const aliasModal = document.getElementById('aliases-modal');
        const launchersModal = document.getElementById('launchers-modal');
        const settingsModal = document.getElementById('settings-modal');
        if (!pwModal.classList.contains('hidden')) {
            e.preventDefault();
            closePasswordDialog();
        } else if (!aliasModal.classList.contains('hidden')) {
            e.preventDefault();
            closeAliasesDialog();
        } else if (!launchersModal.classList.contains('hidden')) {
            e.preventDefault();
            closeLaunchersDialog();
        } else if (!settingsModal.classList.contains('hidden')) {
            e.preventDefault();
            closeSettingsDialog();
        }
    });

    // Resolve docker mode before the first render — renderAppList reads the
    // flag when deciding whether OPEN buttons exist, so ordering matters.
    initDockerMode().then(refreshApps);
});

// ---------------------------------------------------------------------------
// Password dialog
// ---------------------------------------------------------------------------

// Track the password as it was when the dialog opened. The Apply button is
// gated on the input differing from this snapshot — typing the same value back
// in keeps Apply disabled, matching the convention from set_alias.
let pwOriginalValue = '';

async function openPasswordDialog() {
    const result = await app.call('get_password');
    pwOriginalValue = result.password || '';

    const input = document.getElementById('password-input');
    input.value = pwOriginalValue;
    input.placeholder = 'Enter password to enable authentication';

    updatePasswordDialogState();

    const modal = document.getElementById('password-modal');
    modal.classList.remove('hidden');
    // Focus and select so the user can immediately start typing to replace,
    // or just glance at the current value and cancel.
    input.focus();
    input.select();
}

function closePasswordDialog() {
    document.getElementById('password-modal').classList.add('hidden');
}

// Backdrop click dismisses, but only when the click started on the backdrop —
// guards against a drag-select inside the input that ends outside the panel.
function onModalBackdropClick(event) {
    if (event.target.id === 'password-modal') {
        closePasswordDialog();
    }
}

// Re-evaluate Apply enable state and warning visibility whenever the input
// changes. Also called once on dialog open to set the initial state.
function updatePasswordDialogState() {
    const input = document.getElementById('password-input');
    const applyBtn = document.getElementById('btn-pw-apply');
    const warning = document.getElementById('password-note-warning');

    const current = input.value;
    // Apply is enabled when the value has actually changed. Whitespace-only
    // and empty are treated identically as "clear" by the backend, so for the
    // purpose of the changed-check we compare trimmed values when either side
    // would be a clear.
    const currentIsClear = current.trim() === '';
    const originalIsClear = pwOriginalValue.trim() === '';
    let changed;
    if (currentIsClear && originalIsClear) {
        changed = false;  // both effectively empty — no-op
    } else if (currentIsClear || originalIsClear) {
        changed = true;   // one is empty, the other isn't
    } else {
        changed = current !== pwOriginalValue;  // both have content, compare verbatim
    }
    applyBtn.disabled = !changed;

    // Warning shows only in the destructive transition: currently has a
    // password, about to apply an empty one. Going from no-auth to no-auth
    // doesn't warrant a warning, and going from password A to password B is
    // just a change.
    const willDisableAuth = currentIsClear && !originalIsClear;
    warning.classList.toggle('hidden', !willDisableAuth);
}

async function applyPasswordDialog() {
    const input = document.getElementById('password-input');
    // Send the value verbatim — backend handles whitespace-only as the
    // clear-auth gesture. Don't trim here; passwords with intentional
    // leading/trailing spaces are a legitimate (if unusual) choice.
    try {
        await app.call('set_password', { password: input.value });
    } catch (e) {
        // Surface failure inline rather than silently dismissing
        const warning = document.getElementById('password-note-warning');
        warning.textContent = 'Could not save: ' + (e.message || 'unknown error');
        warning.classList.remove('hidden');
        return;
    }
    closePasswordDialog();
}

// Wire the input's events once the DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('password-input');
    input.addEventListener('input', updatePasswordDialogState);
    input.addEventListener('keydown', (e) => {
        // Enter applies if Apply is enabled — symmetric with the alias rename
        // input's behavior. Esc is handled by the document-level listener.
        if (e.key === 'Enter') {
            e.preventDefault();
            const applyBtn = document.getElementById('btn-pw-apply');
            if (!applyBtn.disabled) applyPasswordDialog();
        }
    });
});

// ---------------------------------------------------------------------------
// App Aliases dialog
// ---------------------------------------------------------------------------

// Draft state for the aliases panel. Maps anchor name -> string the user is
// currently typing. Loaded fresh from get_apps() when the dialog opens, edited
// in place as the user types, compared to `aliasesLoaded` to decide whether
// Apply is enabled and what to actually write on commit.
let aliasesDraft = {};
// Snapshot of what was in the system when the dialog opened. Anchor name ->
// effective alias string (alias if set, empty string if not). Apply diffs
// against this; Cancel discards the draft entirely.
let aliasesLoaded = {};

async function openAliasesDialog() {
    // Pull a fresh app list so we work against the current state, not a
    // cached one that might be stale from a partial refresh cycle.
    const apps = await app.call('get_apps');

    aliasesDraft = {};
    aliasesLoaded = {};
    apps.forEach(a => {
        // Effective alias: empty string when none set, the alias verbatim
        // when set. This matches how get_password normalizes its empty case
        // and keeps the diffing logic simple.
        const current = a.alias || '';
        aliasesLoaded[a.name] = current;
        aliasesDraft[a.name] = current;
    });

    renderAliasesTable(apps);
    clearAliasesError();
    updateAliasesApplyButton();

    document.getElementById('aliases-modal').classList.remove('hidden');
}

function closeAliasesDialog() {
    // Cancel is non-destructive — drafts are local state, just dropping them
    // here is sufficient to "undo" any pending edits.
    document.getElementById('aliases-modal').classList.add('hidden');
    aliasesDraft = {};
    aliasesLoaded = {};
    clearAliasesError();
}

// Backdrop click dismisses, but only when the click started on the backdrop —
// guards against drag-select inside an input that ends outside the panel.
function onAliasesBackdropClick(event) {
    if (event.target.id === 'aliases-modal') {
        closeAliasesDialog();
    }
}

function renderAliasesTable(apps) {
    const tbody = document.getElementById('aliases-tbody');
    tbody.innerHTML = '';
    apps.forEach(a => {
        const tr = document.createElement('tr');

        const anchorTd = document.createElement('td');
        anchorTd.className = 'col-anchor';
        anchorTd.textContent = a.name;
        tr.appendChild(anchorTd);

        const aliasTd = document.createElement('td');
        aliasTd.className = 'col-alias';
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'alias-input';
        input.value = aliasesDraft[a.name] || '';
        input.placeholder = 'no alias';
        input.spellcheck = false;
        input.maxLength = 80;
        input.addEventListener('input', () => {
            aliasesDraft[a.name] = input.value;
            updateAliasesApplyButton();
        });
        // Enter from any row applies the whole panel (matches password modal).
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const applyBtn = document.getElementById('btn-aliases-apply');
                if (!applyBtn.disabled) applyAliasesDialog();
            }
        });
        aliasTd.appendChild(input);
        tr.appendChild(aliasTd);

        tbody.appendChild(tr);
    });
}

// Apply is enabled when any row's draft differs from its loaded value, using
// the same normalization the backend uses: empty/whitespace/anchor-equal all
// count as "no alias" and are equivalent to each other for diff purposes.
function updateAliasesApplyButton() {
    const applyBtn = document.getElementById('btn-aliases-apply');
    let anyChanged = false;
    for (const name of Object.keys(aliasesDraft)) {
        if (aliasDraftDiffersFromLoaded(name)) {
            anyChanged = true;
            break;
        }
    }
    applyBtn.disabled = !anyChanged;
}

function aliasDraftDiffersFromLoaded(name) {
    const draft = normalizeAliasForDiff(aliasesDraft[name], name);
    const loaded = normalizeAliasForDiff(aliasesLoaded[name], name);
    return draft !== loaded;
}

// Normalize a value for the diff: trim whitespace, treat anchor-equal as
// empty (since the backend will also treat it as a clear). This means
// retyping the anchor name into a blank field is correctly seen as "no
// change" rather than as "set the alias to the anchor."
function normalizeAliasForDiff(value, anchorName) {
    if (value == null) return '';
    const trimmed = value.trim();
    if (!trimmed) return '';
    if (trimmed === anchorName) return '';
    return trimmed;
}

async function applyAliasesDialog() {
    clearAliasesError();
    // Iterate in deterministic order so error messages reference rows in the
    // same order the user sees them.
    const names = Object.keys(aliasesDraft).sort();
    const failures = [];
    for (const name of names) {
        if (!aliasDraftDiffersFromLoaded(name)) continue;
        // Send the value verbatim — backend handles normalization the same
        // way set_alias does for the inline rename. Empty/whitespace/anchor-
        // equal all clear the entry.
        try {
            await app.call('set_alias', { name, alias: aliasesDraft[name] });
            // Update loaded value so a partial-success retry won't try to
            // re-write rows that already succeeded.
            aliasesLoaded[name] = aliasesDraft[name];
        } catch (e) {
            failures.push({ name, error: e.message || 'unknown error' });
        }
    }

    if (failures.length > 0) {
        // Stay open so the user can see what failed and retry. Recompute
        // Apply state — successful rows are no longer in the diff, so if all
        // failures are also resolved (e.g., user fixed something in another
        // way) Apply naturally disables.
        const summary = failures.map(f => `${f.name}: ${f.error}`).join('; ');
        showAliasesError('Could not save: ' + summary);
        updateAliasesApplyButton();
        return;
    }

    // Success — close the modal and refresh the sidebar so display names,
    // sort order, and tooltips all update to reflect the new state.
    closeAliasesDialog();
    await refreshApps();
}

function showAliasesError(message) {
    const el = document.getElementById('aliases-error');
    el.textContent = message;
    el.classList.remove('hidden');
}

function clearAliasesError() {
    const el = document.getElementById('aliases-error');
    el.textContent = '';
    el.classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Launchers dialog (.desktop file management)
// ---------------------------------------------------------------------------

// Same draft/loaded pattern as the aliases panel: snapshot the on-disk state
// when the dialog opens, edit the draft in place, diff against the snapshot
// to decide whether Apply is enabled, and Cancel discards the draft entirely.
let launchersDraft = {};   // anchor -> {start_menu, desktop, category, terminal}
let launchersLoaded = {};  // anchor -> same shape, snapshotted at open
let launchersUvPath = null;
let launchersUseUv = true; // True when Run-using-uv is enabled (the default)
let launchersCategories = [];

async function openLaunchersDialog() {
    const result = await app.call('get_launchers');
    launchersUvPath = result.uv_path;
    launchersUseUv = result.use_uv !== false;  // default to true if absent
    launchersCategories = result.categories || [];

    launchersDraft = {};
    launchersLoaded = {};
    (result.launchers || []).forEach(l => {
        const state = {
            start_menu: l.start_menu,
            desktop: l.desktop,
            category: l.category,
            terminal: l.terminal,
            tray: l.tray,
        };
        launchersLoaded[l.name] = { ...state };
        launchersDraft[l.name] = { ...state };
    });

    renderLaunchersTable(result.launchers || []);
    renderUvBanner();
    clearLaunchersError();
    updateLaunchersApplyButton();

    document.getElementById('launchers-modal').classList.remove('hidden');
}

function closeLaunchersDialog() {
    document.getElementById('launchers-modal').classList.add('hidden');
    launchersDraft = {};
    launchersLoaded = {};
    clearLaunchersError();
}

function onLaunchersBackdropClick(event) {
    if (event.target.id === 'launchers-modal') {
        closeLaunchersDialog();
    }
}

function renderLaunchersTable(launchers) {
    const tbody = document.getElementById('launchers-tbody');
    tbody.innerHTML = '';
    launchers.forEach(l => {
        const tr = document.createElement('tr');

        // Display name (alias-or-anchor) is what the user thinks of the app
        // as. Anchor surfaces as a hover tooltip only when an alias is in
        // effect — when there isn't one, the visible cell already shows the
        // anchor and a tooltip would be redundant. Same pattern as the
        // sidebar's app rows.
        const displayName = l.alias || l.name;
        const nameTd = document.createElement('td');
        nameTd.className = 'col-anchor';
        nameTd.textContent = displayName;
        if (l.alias) {
            nameTd.title = l.name;
        }
        tr.appendChild(nameTd);

        tr.appendChild(makeToggleTd(l.name, 'start_menu'));
        tr.appendChild(makeToggleTd(l.name, 'desktop'));
        tr.appendChild(makeCategoryTd(l.name));
        tr.appendChild(makeToggleTd(l.name, 'terminal'));
        tr.appendChild(makeToggleTd(l.name, 'tray'));

        tbody.appendChild(tr);
    });
}

function makeToggleTd(name, field) {
    const td = document.createElement('td');
    td.className = 'col-toggle';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'launcher-checkbox';
    cb.checked = !!launchersDraft[name][field];
    cb.addEventListener('change', () => {
        launchersDraft[name][field] = cb.checked;
        updateLaunchersApplyButton();
    });
    td.appendChild(cb);
    return td;
}

function makeCategoryTd(name) {
    const td = document.createElement('td');
    td.className = 'col-category';
    const sel = document.createElement('select');
    sel.className = 'launcher-category-select';
    launchersCategories.forEach(cat => {
        const opt = document.createElement('option');
        opt.value = cat;
        opt.textContent = cat;
        if (cat === launchersDraft[name].category) opt.selected = true;
        sel.appendChild(opt);
    });
    sel.addEventListener('change', () => {
        launchersDraft[name].category = sel.value;
        updateLaunchersApplyButton();
    });
    td.appendChild(sel);
    return td;
}

// Banner above the table reports whether uv was found, BUT only when
// Run-using-uv mode is enabled. In no-uv mode, uv isn't required to create
// launchers, so the warning is suppressed entirely — there's no problem
// to report. To change uv mode, the user goes to the Settings cogwheel.
function renderUvBanner() {
    const banner = document.getElementById('launchers-uv-banner');
    if (!launchersUseUv || launchersUvPath) {
        // Either uv isn't needed (no-uv mode) or uv is found — either way
        // the banner has nothing useful to say. Hide it.
        banner.textContent = '';
        banner.classList.add('hidden');
    } else {
        banner.textContent = '\u24D8 uv not found — launchers cannot be created until uv is installed and in $PATH.';
        banner.classList.remove('hidden');
    }
}

function launcherRowDiffers(name) {
    const d = launchersDraft[name];
    const l = launchersLoaded[name];
    return d.start_menu !== l.start_menu
        || d.desktop !== l.desktop
        || d.category !== l.category
        || d.terminal !== l.terminal
        || d.tray !== l.tray;
}

// Split helpers — launcher writes (Start Menu, Desktop, Category) go to
// set_launchers; tray and terminal writes go to set_tray_flags and
// set_terminal_flags respectively. Each kind of change has different
// validity rules and writes to a different file. The frontend needs to
// know which kind of change is pending per row so the right backend
// methods get called on Apply.
function rowHasLauncherChange(name) {
    const d = launchersDraft[name];
    const l = launchersLoaded[name];
    return d.start_menu !== l.start_menu
        || d.desktop !== l.desktop
        || d.category !== l.category;
}

function rowHasTrayChange(name) {
    return launchersDraft[name].tray !== launchersLoaded[name].tray;
}

function rowHasTerminalChange(name) {
    return launchersDraft[name].terminal !== launchersLoaded[name].terminal;
}

function anyLauncherChangePending() {
    return Object.keys(launchersDraft).some(rowHasLauncherChange);
}

function anyTrayChangePending() {
    return Object.keys(launchersDraft).some(rowHasTrayChange);
}

function anyTerminalChangePending() {
    return Object.keys(launchersDraft).some(rowHasTerminalChange);
}

function updateLaunchersApplyButton() {
    const btn = document.getElementById('btn-launchers-apply');
    const hasLauncherChanges = anyLauncherChangePending();
    const hasTrayChanges = anyTrayChangePending();
    const hasTerminalChanges = anyTerminalChangePending();

    // Apply enables when there's anything to apply AND we can apply it.
    // Launcher changes need uv to be present *unless* we're in no-uv mode
    // (Exec= line is just the script path, no uv invocation). Tray and
    // terminal writes don't need uv either way (they're hearth.json-only).
    // Terminal-flag changes trigger launcher refreshes server-side which
    // would need uv in uv mode, but those are best-effort — Apply doesn't
    // gate on them.
    const launcherApplicable = hasLauncherChanges && (!launchersUseUv || !!launchersUvPath);
    const trayApplicable = hasTrayChanges;
    const terminalApplicable = hasTerminalChanges;

    btn.disabled = !(launcherApplicable || trayApplicable || terminalApplicable);
}

async function applyLaunchersDialog() {
    clearLaunchersError();

    // Build three separate change lists — launcher (Start Menu / Desktop /
    // Category), tray, and terminal. Each goes to its own backend method
    // because the three categories of state live in different places:
    //   - launcher fields → .desktop files (need uv)
    //   - tray flag → hearth.json apps.<name>.tray
    //   - terminal flag → hearth.json apps.<name>.terminal (also triggers
    //     a launcher refresh server-side so the Terminal= line tracks)
    const launcherChanges = [];
    const trayChanges = [];
    const terminalChanges = [];
    Object.keys(launchersDraft).sort().forEach(name => {
        if (rowHasLauncherChange(name)) {
            launcherChanges.push({
                name,
                start_menu: launchersDraft[name].start_menu,
                desktop: launchersDraft[name].desktop,
                category: launchersDraft[name].category,
            });
        }
        if (rowHasTrayChange(name)) {
            trayChanges.push({ name, tray: launchersDraft[name].tray });
        }
        if (rowHasTerminalChange(name)) {
            terminalChanges.push({ name, terminal: launchersDraft[name].terminal });
        }
    });

    if (launcherChanges.length === 0
        && trayChanges.length === 0
        && terminalChanges.length === 0) return;

    // Apply all three halves independently. Each one's success/failure is
    // tracked separately so a row that applied in one but failed in another
    // has its loaded snapshot correctly synced for the parts that worked.
    let launcherApplied = [];
    let launcherFailures = [];
    let trayApplied = [];
    let trayFailures = [];
    let terminalApplied = [];
    let terminalFailures = [];

    // Apply terminal FIRST — terminal changes trigger server-side launcher
    // refreshes that read from hearth.json, so the terminal flag needs to
    // be persisted before we apply launcher changes (or the launcher's
    // Terminal= line would temporarily reflect the old flag).
    if (terminalChanges.length > 0) {
        let result;
        try {
            result = await app.call('set_terminal_flags', { changes: terminalChanges });
        } catch (e) {
            terminalFailures.push({ name: '(all)', error: e.message || 'terminal save failed' });
            result = null;
        }
        if (result) {
            terminalApplied = result.applied || [];
            terminalFailures = result.failures || [];
        }
    }

    if (launcherChanges.length > 0) {
        let result;
        try {
            result = await app.call('set_launchers', { changes: launcherChanges });
        } catch (e) {
            launcherFailures.push({ name: '(all)', error: e.message || 'launcher save failed' });
            result = null;
        }
        if (result) {
            if (result.error) {
                // Whole-batch error (uv missing) — every launcher change failed
                launcherChanges.forEach(c => {
                    launcherFailures.push({ name: c.name, error: result.error });
                });
            } else {
                launcherApplied = result.applied || [];
                launcherFailures = result.failures || [];
            }
        }
    }

    if (trayChanges.length > 0) {
        let result;
        try {
            result = await app.call('set_tray_flags', { changes: trayChanges });
        } catch (e) {
            trayFailures.push({ name: '(all)', error: e.message || 'tray save failed' });
            result = null;
        }
        if (result) {
            trayApplied = result.applied || [];
            trayFailures = result.failures || [];
        }
    }

    // Sync loaded snapshot for applied rows so retries diff against the
    // new on-disk state. Each kind of field syncs independently — a row
    // may have applied in some halves but failed in others.
    new Set(launcherApplied).forEach(name => {
        launchersLoaded[name].start_menu = launchersDraft[name].start_menu;
        launchersLoaded[name].desktop = launchersDraft[name].desktop;
        launchersLoaded[name].category = launchersDraft[name].category;
    });
    new Set(trayApplied).forEach(name => {
        launchersLoaded[name].tray = launchersDraft[name].tray;
    });
    new Set(terminalApplied).forEach(name => {
        launchersLoaded[name].terminal = launchersDraft[name].terminal;
    });

    const allFailures = [...launcherFailures, ...trayFailures, ...terminalFailures];
    if (allFailures.length > 0) {
        const summary = allFailures.map(f => `${f.name}: ${f.error}`).join('; ');
        showLaunchersError('Some changes failed: ' + summary);
        updateLaunchersApplyButton();
        return;
    }

    closeLaunchersDialog();
}

function showLaunchersError(message) {
    const el = document.getElementById('launchers-error');
    el.textContent = message;
    el.classList.remove('hidden');
}

function clearLaunchersError() {
    const el = document.getElementById('launchers-error');
    el.textContent = '';
    el.classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Hearth Monitor Settings dialog (cogwheel)
// ---------------------------------------------------------------------------
//
// The cogwheel opens a dedicated modal for the monitor's own preferences —
// kept separate from the operated-apps panels (Aliases, Launchers) so the
// operator-vs-operated split is preserved in the UI. Currently holds the
// monitor's launcher controls (Start Menu / Desktop / Category / Terminal)
// and its tray flag. Layout is a vertical form rather than a table since
// it's about a single subject.

let settingsDraft = {};
let settingsLoaded = {};
let settingsUvPath = null;

async function openSettingsDialog() {
    // Docker mode: every control in this modal (uv toggle, launcher rows,
    // tray) is a local-machine concern with no meaning in the container.
    // The modal shell still opens — the control stays discoverable and
    // explains itself, and remains the home for any future docker-specific
    // settings — but shows an honest notice instead of the form. Apply is
    // hidden (nothing can be applied) and Cancel reads Close. dockerMode
    // never changes mid-session, but both branches set full state so the
    // dialog is correct regardless of history.
    const form = document.getElementById('settings-form');
    const dockerNote = document.getElementById('settings-docker-note');
    const applyBtn = document.getElementById('btn-settings-apply');
    const cancelBtn = document.getElementById('btn-settings-cancel');
    if (dockerMode) {
        form.classList.add('hidden');
        dockerNote.classList.remove('hidden');
        applyBtn.classList.add('hidden');
        cancelBtn.textContent = 'Close';
        clearSettingsError();
        document.getElementById('settings-modal').classList.remove('hidden');
        return;
    }
    form.classList.remove('hidden');
    dockerNote.classList.add('hidden');
    applyBtn.classList.remove('hidden');
    cancelBtn.textContent = 'Cancel';

    const result = await app.call('get_monitor_settings');
    settingsUvPath = result.uv_path;

    settingsLoaded = {
        use_uv: result.use_uv,
        start_menu: result.start_menu,
        desktop: result.desktop,
        category: result.category,
        terminal: result.terminal,
        tray: result.tray,
    };
    settingsDraft = { ...settingsLoaded };

    // Populate the category dropdown with the valid options
    const sel = document.getElementById('settings-category');
    sel.innerHTML = '';
    (result.categories || []).forEach(cat => {
        const opt = document.createElement('option');
        opt.value = cat;
        opt.textContent = cat;
        if (cat === settingsDraft.category) opt.selected = true;
        sel.appendChild(opt);
    });

    // Populate form controls from draft
    document.getElementById('settings-use-uv').checked = settingsDraft.use_uv;
    document.getElementById('settings-start-menu').checked = settingsDraft.start_menu;
    document.getElementById('settings-desktop').checked = settingsDraft.desktop;
    document.getElementById('settings-terminal').checked = settingsDraft.terminal;
    document.getElementById('settings-tray').checked = settingsDraft.tray;

    renderSettingsUvBanner();
    clearSettingsError();
    updateSettingsApplyButton();

    document.getElementById('settings-modal').classList.remove('hidden');
}

function closeSettingsDialog() {
    document.getElementById('settings-modal').classList.add('hidden');
    settingsDraft = {};
    settingsLoaded = {};
    clearSettingsError();
}

function onSettingsBackdropClick(event) {
    if (event.target.id === 'settings-modal') {
        closeSettingsDialog();
    }
}

function renderSettingsUvBanner() {
    const banner = document.getElementById('settings-uv-banner');
    // Banner content depends on use_uv mode (from the draft, so it reflects
    // what the user is currently choosing in the dialog, not just what's
    // persisted). Three states:
    //
    //   1. uv mode ON, uv installed → banner hidden (happy path)
    //   2. uv mode ON, uv missing → orange warning, can't create launchers
    //   3. uv mode OFF → informational warning about user responsibilities
    if (settingsDraft.use_uv === false) {
        banner.textContent = '\u24D8 Python must be in $PATH and required Python packages must be managed manually.';
        banner.classList.remove('hidden');
    } else if (settingsUvPath) {
        banner.textContent = '';
        banner.classList.add('hidden');
    } else {
        banner.textContent = '\u24D8 uv not found — launcher cannot be created until uv is installed and in $PATH.';
        banner.classList.remove('hidden');
    }
}

// Read the form's current state into the draft. Called by every input's
// change listener so the draft tracks the UI live.
function refreshSettingsDraft() {
    settingsDraft.use_uv = document.getElementById('settings-use-uv').checked;
    settingsDraft.start_menu = document.getElementById('settings-start-menu').checked;
    settingsDraft.desktop = document.getElementById('settings-desktop').checked;
    settingsDraft.category = document.getElementById('settings-category').value;
    settingsDraft.terminal = document.getElementById('settings-terminal').checked;
    settingsDraft.tray = document.getElementById('settings-tray').checked;
    // Banner content depends on the draft's use_uv field — re-render so
    // toggling the checkbox immediately switches the warning text.
    renderSettingsUvBanner();
    updateSettingsApplyButton();
}

// Apply enables when any field differs from the loaded snapshot. The
// "Takes effect on next launch" note shows in the same condition — it's
// only relevant when there's something pending that would actually take
// effect on the next launch.
function settingsDiffers() {
    return settingsDraft.use_uv !== settingsLoaded.use_uv
        || settingsDraft.start_menu !== settingsLoaded.start_menu
        || settingsDraft.desktop !== settingsLoaded.desktop
        || settingsDraft.category !== settingsLoaded.category
        || settingsDraft.terminal !== settingsLoaded.terminal
        || settingsDraft.tray !== settingsLoaded.tray;
}

function updateSettingsApplyButton() {
    const btn = document.getElementById('btn-settings-apply');
    const note = document.getElementById('settings-takes-effect');
    const changed = settingsDiffers();

    // Apply requires changes AND (uv mode is off OR uv is found).
    // In no-uv mode, launcher writes don't need uv at all. In uv mode,
    // the launcher half can't proceed without uv being on PATH, so we
    // disable Apply outright to keep the user from accidentally applying
    // a half-state.
    const uvBlocking = settingsDraft.use_uv !== false && !settingsUvPath;
    btn.disabled = !changed || uvBlocking;

    // The "next launch" note shows when there's a change pending. Hidden
    // when the form matches loaded state — there's nothing pending so the
    // disclaimer would be misleading.
    note.classList.toggle('hidden', !changed);
}

async function applySettingsDialog() {
    clearSettingsError();
    let result;
    try {
        result = await app.call('set_monitor_settings', { settings: settingsDraft });
    } catch (e) {
        showSettingsError('Could not save: ' + (e.message || 'unknown error'));
        return;
    }

    // set_monitor_settings always returns an object — failures populate
    // result.failures. Tray success is reflected in result.tray.
    if (result.failures && result.failures.length > 0) {
        // Keep open, surface the per-failure message. Successful halves
        // are already persisted; updating loaded reflects what's now on
        // disk so retry diffs against the new baseline.
        if (result.applied && result.applied.length > 0) {
            // Launcher succeeded, even if tray didn't (or vice versa).
            // Sync loaded to whatever we know about.
            settingsLoaded.start_menu = settingsDraft.start_menu;
            settingsLoaded.desktop = settingsDraft.desktop;
            settingsLoaded.category = settingsDraft.category;
            settingsLoaded.terminal = settingsDraft.terminal;
        }
        if (result.tray !== null && result.tray !== undefined) {
            settingsLoaded.tray = result.tray;
        }
        const summary = result.failures.map(f => f.error).join('; ');
        showSettingsError(summary);
        updateSettingsApplyButton();
        return;
    }

    closeSettingsDialog();
}

function showSettingsError(message) {
    const el = document.getElementById('settings-error');
    el.textContent = message;
    el.classList.remove('hidden');
}

function clearSettingsError() {
    const el = document.getElementById('settings-error');
    el.textContent = '';
    el.classList.add('hidden');
}

// Wire the form inputs once the DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    ['settings-use-uv', 'settings-start-menu', 'settings-desktop', 'settings-category',
     'settings-terminal', 'settings-tray'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', refreshSettingsDraft);
    });
});

// ---------------------------------------------------------------------------
// Open in local mode + toast notifications
// ---------------------------------------------------------------------------
//
// The Open button on each app row launches the app in local mode (its own
// pywebview window), as opposed to the existing Serve/Stop buttons which
// manage serve mode. Open is fire-and-forget — the monitor doesn't track
// the spawned process; the framework's single-instance lockfile prevents
// duplicate launches. Feedback is delivered via toasts (transient messages
// that appear briefly in a corner and fade out).

async function openApp(name, displayName) {
    // The anchor `name` is the backend identity (filenames, lockfiles,
    // hearth.json keys); `displayName` is what the user sees and is what
    // belongs in toast messages. Caller passes both since the row already
    // has both values. Fall back to anchor if displayName wasn't provided
    // so this function stays safe to call from anywhere.
    const label = displayName || name;
    let result;
    try {
        result = await app.call('open_app', { name });
    } catch (e) {
        showToast('Could not open ' + label + ': ' + (e.message || 'unknown error'), 'error');
        return;
    }
    if (result.error) {
        showToast(label + ': ' + result.error, 'error');
        return;
    }
    if (result.already_open) {
        showToast(label + ' is already open', 'info');
        return;
    }
    if (result.opened) {
        showToast('Opening ' + label + '…', 'success');
        return;
    }
    // Unrecognized response shape — surface something rather than fail silently
    showToast(label + ': unexpected response', 'error');
}

// Toast notifications. Stacks at the bottom-right of the viewport; each toast
// fades out after a few seconds. Multiple toasts can be visible simultaneously
// (newer ones stack on top of older ones). Existing identical toasts are not
// deduplicated — if the user clicks Open twice in a row, they see two
// toasts, which honestly conveys what happened.

function showToast(message, kind) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = 'toast toast-' + (kind || 'info');
    toast.textContent = message;
    container.appendChild(toast);

    // Trigger the fade-in animation by adding the visible class after a
    // microtask. Without this delay, the browser merges the initial style
    // and the post-class style into a single render and the transition
    // doesn't fire.
    requestAnimationFrame(() => {
        toast.classList.add('toast-visible');
    });

    // Auto-dismiss after a few seconds. The fade-out animation runs first
    // (CSS transition on opacity); we then remove the element after it
    // completes so it doesn't accumulate in the DOM.
    setTimeout(() => {
        toast.classList.remove('toast-visible');
        toast.classList.add('toast-leaving');
        // Wait for the transition to complete before removing from DOM
        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 300);
    }, kind === 'error' ? 5000 : 3000);  // errors stay visible longer
}
