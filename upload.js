'use strict';
// windrv upload page (upload.php): drop a driver folder, review the drivers per device class,
// upload only the files of the selected drivers and import them.

//
// pure helpers (tested with node: tests/upload.test.js)
//
const UploadModel = {
    formatSize(bytes) {
        for (const unit of ['B', 'KB', 'MB', 'GB']) {
            if (bytes < 1024 || unit === 'GB') {
                return (unit === 'B' ? bytes : bytes.toFixed(1)) + ' ' + unit;
            }
            bytes /= 1024;
        }
    },

    // class names are case-insensitive, classes maps the Microsoft class names to their device category
    classInfo(name, classes) {
        const key = (name || '').toLowerCase();
        const known = Object.keys(classes).find(c => c.toLowerCase() === key);
        if (!key) return {key, name: '', label: '(no class)'};
        return known
            ? {key, name: known, label: `${classes[known]} (${known})`}
            : {key, name, label: name};
    },

    // device classes of the drivers that can be imported, sorted by label
    classGroups(drivers, classes) {
        const groups = new Map();
        for (const driver of drivers.filter(d => d.skipped === null)) {
            const info = UploadModel.classInfo(driver.class, classes);
            if (!groups.has(info.key)) groups.set(info.key, {...info, drivers: 0, devices: 0});
            const group = groups.get(info.key);
            group.drivers += 1;
            group.devices += driver.hardwareIds;
        }
        return [...groups.values()].sort((a, b) => a.label.localeCompare(b.label));
    },

    classKey(name) {
        return (name || '').toLowerCase();
    },

    // network adapters are what windrv is for, otherwise everything
    defaultClasses(groups) {
        const net = groups.find(g => g.key === 'net');
        return new Set(net ? [net.key] : groups.map(g => g.key));
    },

    // INF files of the drivers of the given classes
    driversOfClasses(plan, classKeys) {
        return new Set(plan.drivers.filter(d => d.skipped === null && classKeys.has(UploadModel.classKey(d.class))).map(d => d.inf));
    },

    // whether 'all', 'some' or 'none' of the drivers of a class are selected
    classState(plan, classKey, selectedInfs) {
        const infs = [...UploadModel.driversOfClasses(plan, new Set([classKey]))];
        const selected = infs.filter(inf => selectedInfs.has(inf)).length;
        return selected === 0 ? 'none' : selected === infs.length ? 'all' : 'some';
    },

    // the selected drivers and the files they require
    selection(plan, selectedInfs, sizes, uploaded) {
        const drivers = plan.drivers.filter(d => d.skipped === null && selectedInfs.has(d.inf));
        const files = [...new Set(drivers.flatMap(d => d.files))].sort();
        const pending = files.filter(f => !uploaded.has(f));
        const sum = list => list.reduce((total, f) => total + (sizes.get(f) || 0), 0);
        return {
            drivers,
            files,
            size: sum(files),
            pending,
            pendingSize: sum(pending),
            missing: [...new Set(drivers.flatMap(d => d.missing))].sort(),
            unsigned: drivers.filter(d => !d.signed).length,
        };
    },
};

if (typeof module !== 'undefined') {
    module.exports = UploadModel;
}

//
// page
//
if (typeof document !== 'undefined') {
    const CHUNK = 4 * 1024 * 1024;
    const CLASSES = window.WINDRV_CLASSES || {};
    const $ = id => document.getElementById(id);
    const size = UploadModel.formatSize;

    const state = {
        files: new Map(),       // relative path -> File
        session: null,
        plan: null,
        groups: [],
        selected: new Set(),    // INF files of the selected drivers
        uploaded: new Set(),    // completely uploaded paths
        busy: false,
    };

    function el(tag, attrs = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (key === 'class') node.className = value;
            else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
            else if (value === true) node.setAttribute(key, '');
            else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value);
        }
        for (const child of children.flat()) {
            if (child !== null && child !== undefined && child !== false) {
                node.append(child instanceof Node ? child : document.createTextNode(String(child)));
            }
        }
        return node;
    }

    function status(kind, ...content) {
        $('status').replaceChildren(content.length ? el('div', {class: `notice ${kind}`}, ...content) : '');
    }

    function progress(label) {
        const bar = el('progress', {max: 1, value: 0});
        const text = el('div', {class: 'muted small'});
        status('info', el('div', {class: 'progress-label'}, label), bar, text);
        return (done, total, detail) => {
            bar.max = total || 1;
            bar.value = done;
            text.textContent = `${size(done)} of ${size(total)}` + (detail ? ` - ${detail}` : '');
        };
    }

    async function post(action, params = {}, body = null) {
        const query = new URLSearchParams({action, ...params});
        const response = await fetch('upload.php?' + query, {method: 'POST', body});
        const data = await response.json().catch(() => ({error: `invalid response (${response.status})`}));
        if (!response.ok || data.error) {
            const error = new Error(data.error || response.statusText);
            error.details = [data.output, ...(data.files || [])].filter(Boolean).join('\n');
            throw error;
        }
        return data;
    }

    async function uploadFiles(paths, report) {
        const total = paths.reduce((sum, p) => sum + state.files.get(p).size, 0);
        let done = 0;
        for (const path of paths) {
            const file = state.files.get(path);
            let offset = 0;
            do {
                const chunk = file.slice(offset, offset + CHUNK);
                await post('file', {session: state.session, path, offset}, chunk);
                offset += chunk.size;
                done += chunk.size;
                report(done, total, path);
            } while (offset < file.size);
            state.uploaded.add(path);
        }
    }

    //
    // selecting the folder: drop area (drag & drop) or folder picker (click)
    //
    // directory entries of a drop are read recursively; readEntries returns the entries in batches
    async function filesFromEntries(entries) {
        const files = [];
        const read = (fn, ...args) => new Promise((resolve, reject) => fn(...args, resolve, reject));
        async function walk(entry) {
            if (entry.isFile) {
                files.push([entry.fullPath.replace(/^\/+/, ''), await read(entry.file.bind(entry))]);
            } else if (entry.isDirectory) {
                const reader = entry.createReader();
                for (;;) {
                    const batch = await read(reader.readEntries.bind(reader));
                    if (!batch.length) break;
                    for (const child of batch) await walk(child);
                }
            }
        }
        for (const entry of entries) await walk(entry);
        return files;
    }

    const drop = $('drop');
    // the drop area is the label of the picker: a click opens it natively (not while busy)
    drop.addEventListener('click', event => {
        if (state.busy) event.preventDefault();
    });
    drop.addEventListener('keydown', event => {
        if ((event.key === 'Enter' || event.key === ' ') && !state.busy) {
            event.preventDefault();
            $('picker').click();
        }
    });
    for (const type of ['dragenter', 'dragover']) {
        drop.addEventListener(type, event => {
            event.preventDefault();
            if (!state.busy) drop.classList.add('dragover');
        });
    }
    drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
    drop.addEventListener('drop', async event => {
        event.preventDefault();
        drop.classList.remove('dragover');
        if (state.busy) return;
        // the entries have to be taken before the first await, the drop data is gone afterwards
        const entries = [...event.dataTransfer.items].map(item => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
        const plainFiles = [...event.dataTransfer.files].map(f => [f.name, f]);
        analyze(entries.length ? await filesFromEntries(entries) : plainFiles);
    });
    $('picker').addEventListener('change', event => {
        analyze([...event.target.files].map(f => [f.webkitRelativePath || f.name, f]));
        event.target.value = '';
    });
    window.addEventListener('beforeunload', event => {
        if (state.busy) event.preventDefault();
    });

    function folderSummary() {
        const roots = [...new Set([...state.files.keys()].map(p => p.split('/')[0]))];
        const total = [...state.files.values()].reduce((sum, f) => sum + f.size, 0);
        return {roots, total};
    }

    async function cancelSession() {
        if (state.session) {
            const session = state.session;
            state.session = null;
            await post('cancel', {session}).catch(() => {});
        }
    }

    async function analyze(files) {
        await cancelSession();
        state.files = new Map(files);
        state.plan = null;
        state.uploaded = new Set();
        $('review').hidden = true;
        $('actions').hidden = true;

        const {roots, total} = folderSummary();
        $('dropInfo').textContent = state.files.size
            ? `${roots.join(', ')} - ${state.files.size} files, ${size(total)}` : '';
        const infs = [...state.files.keys()].filter(p => p.toLowerCase().endsWith('.inf'));
        if (!infs.length) {
            status('error', 'The selection contains no INF files. Select the extracted driver package folder.');
            return;
        }

        state.busy = true;
        drop.classList.add('busy');
        try {
            const manifest = [...state.files].map(([path, file]) => ({path, size: file.size}));
            const start = await post('start', {}, JSON.stringify({files: manifest}));
            state.session = start.session;
            await uploadFiles(start.infs, progress(`Reading ${start.infs.length} INF file(s) ...`));
            status('info', 'Analyzing the drivers ...');
            const plan = await post('plan', {session: state.session});
            plan.uploaded.forEach(p => state.uploaded.add(p));
            state.plan = plan;
            state.groups = UploadModel.classGroups(plan.drivers, CLASSES);
            state.selected = UploadModel.driversOfClasses(plan, UploadModel.defaultClasses(state.groups));
            status('');
        } catch (error) {
            showError(error);
        } finally {
            state.busy = false;
            drop.classList.remove('busy');
        }
        // rendered after the busy state ended, otherwise the buttons stay disabled
        if (state.plan) render();
    }

    function showError(error) {
        status('error', el('strong', {}, error.message), error.details ? el('pre', {}, error.details) : null);
    }

    //
    // review
    //
    function render() {
        const plan = state.plan;
        const sizes = new Map([...state.files].map(([path, file]) => [path, file.size]));
        const selection = UploadModel.selection(plan, state.selected, sizes, state.uploaded);
        const {total} = folderSummary();
        const importable = plan.drivers.filter(d => d.skipped === null);
        const skipped = plan.drivers.filter(d => d.skipped !== null);

        const card = (value, label, kind = '') => el('div', {class: `card ${kind}`}, el('div', {class: 'card-value'}, value), el('div', {class: 'card-label'}, label));
        const summary = el('div', {class: 'cards'},
            card(state.files.size, `files, ${size(total)}`),
            card(plan.drivers.length, 'INF files'),
            card(importable.length, 'drivers'),
            card(selection.files.length, `files required, ${size(selection.size)}`, 'highlight'),
            selection.missing.length ? card(selection.missing.length, 'files not in the folder (skipped)', 'warning') : null);

        const checkbox = (state, onchange) => {
            const input = el('input', {type: 'checkbox', checked: state === 'all', onchange});
            input.indeterminate = state === 'some';
            return input;
        };
        const toggle = (infs, checked) => {
            infs.forEach(inf => checked ? state.selected.add(inf) : state.selected.delete(inf));
            render();
        };

        // a class selects or deselects all of its drivers
        const chips = state.groups.length ? el('div', {class: 'chips'}, state.groups.map(group => {
            const classState = UploadModel.classState(plan, group.key, state.selected);
            return el('label', {class: 'chip' + (classState !== 'none' ? ' selected' : '')},
                checkbox(classState, event => toggle(UploadModel.driversOfClasses(plan, new Set([group.key])), event.target.checked)),
                el('span', {}, group.label),
                el('span', {class: 'count', title: 'drivers / device ids'}, `${group.drivers} / ${group.devices}`));
        })) : el('p', {class: 'muted'}, 'No drivers with hardware ids found.');

        // every driver can be selected on its own, sorted by class
        const label = d => UploadModel.classInfo(d.class, CLASSES).label;
        const drivers = [...importable].sort((a, b) => label(a).localeCompare(label(b)) || a.inf.localeCompare(b.inf));
        const allState = selection.drivers.length === 0 ? 'none' : selection.drivers.length === importable.length ? 'all' : 'some';
        const rows = drivers.map(d => el('tr', {class: state.selected.has(d.inf) ? '' : 'unselected'},
            el('td', {class: 'select-col'}, checkbox(state.selected.has(d.inf) ? 'all' : 'none', event => toggle([d.inf], event.target.checked))),
            el('td', {}, el('span', {class: 'code'}, d.inf)),
            el('td', {}, label(d), d.provider ? el('div', {class: 'muted small'}, d.provider) : null),
            el('td', {}, d.version || '', d.date ? el('div', {class: 'muted small'}, d.date) : null),
            el('td', {}, el('span', {class: d.signed ? 'badge ok' : 'badge warning',
                title: d.signed ? 'has a catalog file' : 'no catalog file: 64-bit WinPE does not load unsigned drivers'}, d.signed ? 'signed' : 'unsigned')),
            el('td', {}, `${d.hardwareIds}`, el('div', {class: 'muted small'}, d.architectures.join(', '))),
            el('td', {}, `${d.files.length} files`, el('div', {class: 'muted small'}, size(d.files.reduce((s, f) => s + (sizes.get(f) || 0), 0)))),
            el('td', {}, d.missing.length
                ? el('span', {class: 'badge warning', title: 'not part of the folder, skipped:\n' + d.missing.join('\n')}, `${d.missing.length} skipped`)
                : el('span', {class: 'badge ok'}, 'complete'))));
        const table = rows.length
            ? el('table', {}, el('thead', {}, el('tr', {},
                el('th', {class: 'select-col', title: 'select all'}, checkbox(allState, event => toggle(importable.map(d => d.inf), event.target.checked))),
                ['INF', 'Class', 'Version', 'Signature', 'Device ids', 'Required', 'Files'].map(h => el('th', {}, h)))), el('tbody', {}, rows))
            : el('p', {class: 'muted'}, 'No drivers with hardware ids found.');

        const notices = [];
        if (selection.missing.length) {
            // missing files do not prevent the import (e.g. files of optional components or other platforms)
            notices.push(el('div', {class: 'notice warning'},
                el('strong', {}, `${selection.missing.length} file(s) referenced by the selected INF files are not part of the folder and are skipped. `),
                'This is often fine (e.g. optional components); if a driver does not install, check that the complete driver package was selected.',
                el('details', {}, el('summary', {}, 'Skipped files'), el('pre', {}, selection.missing.join('\n')))));
        }
        if (selection.unsigned) {
            notices.push(el('div', {class: 'notice warning'}, `${selection.unsigned} selected driver(s) are unsigned: they are imported, but 64-bit WinPE does not load them.`));
        }

        const messages = plan.drivers.flatMap(d => d.messages.map(m => `${d.inf}: ${m}`));
        // replaceChildren would insert null as the text "null"
        $('review').replaceChildren(...[
            summary,
            el('h2', {}, 'Device classes'),
            chips,
            el('h2', {}, `Drivers (${selection.drivers.length} of ${importable.length} selected)`),
            ...notices,
            table,
            skipped.length ? el('details', {}, el('summary', {}, `INF files that are not imported (${skipped.length})`),
                el('table', {}, el('tbody', {}, skipped.map(d => el('tr', {}, el('td', {}, el('span', {class: 'code'}, d.inf)), el('td', {class: 'muted'}, d.skipped)))))) : null,
            messages.length ? el('details', {}, el('summary', {}, `Scanner messages (${messages.length})`), el('pre', {}, messages.join('\n'))) : null,
        ].filter(node => node !== null));
        $('review').hidden = false;

        $('selection').textContent = selection.drivers.length
            ? `Import ${selection.drivers.length} driver(s): upload ${selection.pending.length} file(s), ${size(selection.pendingSize)} of ${size(total)}`
            : 'No drivers selected';
        $('import').disabled = state.busy || !selection.drivers.length;
        $('cancel').disabled = state.busy;
        $('actions').hidden = false;
        return selection;
    }

    $('cancel').addEventListener('click', async () => {
        await cancelSession();
        state.files = new Map();
        state.plan = null;
        state.selected = new Set();
        state.uploaded = new Set();
        $('review').hidden = true;
        $('actions').hidden = true;
        $('dropInfo').textContent = '';
        status('info', 'Cancelled.');
    });

    $('import').addEventListener('click', async () => {
        const selection = render();
        state.busy = true;
        $('import').disabled = $('cancel').disabled = true;
        drop.classList.add('busy');
        try {
            await uploadFiles(selection.pending, progress(`Uploading ${selection.pending.length} file(s) ...`));
            status('info', 'Importing ...');
            const result = await post('commit', {session: state.session}, JSON.stringify({infs: selection.drivers.map(d => d.inf)}));
            state.session = null;
            $('review').hidden = true;
            $('actions').hidden = true;
            $('dropInfo').textContent = '';
            status('success', el('strong', {}, `Imported ${result.imported.length} driver(s). `),
                el('a', {href: 'index.php'}, 'Show the driver database'),
                el('details', {}, el('summary', {}, 'Scanner output'), el('pre', {}, result.output)));
        } catch (error) {
            showError(error);
        } finally {
            state.busy = false;
            drop.classList.remove('busy');
            // after an error: show what is still to upload (completed files are kept)
            if (state.session) render();
        }
    });
}
