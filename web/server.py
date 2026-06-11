import os
import re
import secrets
import socket
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template_string, request, send_file
from dotenv import set_key
from db.database import TranscriptionDB
from config import APP_DATA_DIR

_ENV_PATH = os.path.join(APP_DATA_DIR, ".env")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["SECRET_KEY"] = secrets.token_hex(32)

# Single DB instance (avoids re-running DDL on every request)
_db = TranscriptionDB()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vflow - Transcripciones</title>
    <!-- NOTE: Tailwind loaded from CDN. Accepted risk: app is local-only, dashboard on localhost. -->
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');
        body { font-family: 'Inter', system-ui, sans-serif; background: #0a0a0a; color: #e5e5e5; }
        .glass { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06); }
        .row-hover:hover { background: rgba(255,255,255,0.05); }
        .text-preview { max-height: 2.6em; overflow: hidden; transition: max-height 0.3s ease; }
        .text-preview.expanded { max-height: 500px; }
        .copied { animation: flash 0.5s ease; }
        @keyframes flash { 0%,100% { background: transparent; } 50% { background: rgba(80,220,120,0.1); } }
        .deleted { animation: fadeout 0.4s ease forwards; }
        @keyframes fadeout { to { opacity: 0; transform: translateX(20px); } }
        .brand-logo { width: 28px; height: 28px; border-radius: 6px; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
        .dropdown { position: relative; display: inline-block; }
        .dropdown-menu { display: none; position: absolute; right: 0; top: 100%; margin-top: 4px;
            background: #1a1a1a; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;
            min-width: 200px; z-index: 50; overflow: hidden; }
        .dropdown.open .dropdown-menu { display: block; }
        .dropdown-item { padding: 8px 14px; font-size: 13px; color: rgba(255,255,255,0.6);
            cursor: pointer; transition: background 0.15s; }
        .dropdown-item:hover { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.9); }
        .dropdown-item.danger { color: #ef4444; }
        .dropdown-item.danger:hover { background: rgba(239,68,68,0.15); }
        .edit-area { width: 100%; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.15);
            border-radius: 6px; color: #e5e5e5; padding: 6px 8px; font-size: 13px; font-family: inherit;
            resize: vertical; min-height: 60px; }
        .edit-area:focus { outline: none; border-color: rgba(140,80,220,0.5); }
        .cfg-select { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
            border-radius: 6px; color: #e5e5e5; padding: 6px 8px; font-size: 13px; width: 100%; cursor: pointer; }
        .cfg-select:focus { outline: none; border-color: rgba(140,80,220,0.5); }
        .cfg-select option { background: #1a1a1a; }
        .toggle-switch { position: relative; display: inline-block; width: 36px; height: 20px; flex-shrink: 0; }
        .toggle-switch input { opacity: 0; width: 0; height: 0; }
        .toggle-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(255,255,255,0.1); border-radius: 20px; transition: 0.2s; }
        .toggle-slider:before { position: absolute; content: ''; height: 14px; width: 14px;
            left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: 0.2s; }
        .toggle-switch input:checked + .toggle-slider { background: rgba(140,80,220,0.6); }
        .toggle-switch input:checked + .toggle-slider:before { transform: translateX(16px); }
        .selection-bar { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
            background: #1a1a1a; border: 1px solid rgba(140,80,220,0.4); border-radius: 12px;
            padding: 10px 20px; display: none; align-items: center; gap: 14px; z-index: 100;
            box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
        .selection-bar.visible { display: flex; }
        .selection-bar .count { color: rgba(255,255,255,0.7); font-size: 13px; }
        .selection-bar button { font-size: 13px; padding: 5px 14px; border-radius: 6px; cursor: pointer; border: none; }
        .selection-bar .del-btn { background: rgba(239,68,68,0.2); color: #f87171; }
        .selection-bar .del-btn:hover { background: rgba(239,68,68,0.35); }
        .selection-bar .cancel-btn { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.5); }
        .selection-bar .cancel-btn:hover { background: rgba(255,255,255,0.15); color: rgba(255,255,255,0.8); }
        tr.selected-row { background: rgba(140,80,220,0.08); }
        tr.row-hover { user-select: none; -webkit-user-select: none; }
    </style>
</head>
<body class="min-h-screen p-6">
    <div class="max-w-4xl mx-auto">
        <!-- Header -->
        <div class="flex items-center justify-between mb-8">
            <div class="flex items-center gap-3">
                <img src="/logo" class="brand-logo" alt="Vflow">
                <div class="text-2xl font-semibold text-white">Vflow</div>
                <span class="text-xs text-white/30 bg-white/5 px-2 py-1 rounded-full" id="count-badge">-</span>
            </div>
            <div class="flex items-center gap-3">
                <input type="text" id="search" placeholder="Buscar..."
                    class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                    placeholder-white/30 focus:outline-none focus:border-white/20 w-48">
                <div class="dropdown" id="cleanup-dropdown">
                    <button onclick="document.getElementById('cleanup-dropdown').classList.toggle('open')"
                        class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5">
                        Limpiar &#9662;
                    </button>
                    <div class="dropdown-menu">
                        <div class="dropdown-item" onclick="bulkDelete('day','hoy')">Eliminar de hoy</div>
                        <div class="dropdown-item" onclick="bulkDelete('week')">Eliminar ultima semana</div>
                        <div class="dropdown-item" onclick="bulkDelete('month')">Eliminar ultimo mes</div>
                        <div class="dropdown-item" onclick="deleteByDate()">Elegir fecha especifica...</div>
                        <div class="dropdown-item danger" onclick="bulkDelete('all')">Eliminar todo</div>
                    </div>
                </div>
                <button onclick="loadData()" class="text-white/40 hover:text-white/70 text-sm">Actualizar</button>
                <button onclick="toggleSettings()" class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5" title="Configuración">&#9881;</button>
            </div>
        </div>

        <!-- Settings panel -->
        <div id="settings-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-4">Configuración</div>
            <div class="grid gap-4" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))">
                <div>
                    <label class="text-xs text-white/40 block mb-1">Idioma de transcripción</label>
                    <select id="cfg-language" class="cfg-select">
                        <option value="es">Español</option>
                        <option value="en">English</option>
                        <option value="fr">Français</option>
                        <option value="de">Deutsch</option>
                        <option value="it">Italiano</option>
                        <option value="pt">Português</option>
                        <option value="ja">日本語</option>
                        <option value="zh">中文</option>
                        <option value="auto">Auto-detectar (mayor costo)</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Micrófono</label>
                    <select id="cfg-microphone" class="cfg-select">
                        <option value="">Sistema por defecto</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Idioma de salida (traducción)</label>
                    <select id="cfg-translate-target" class="cfg-select">
                        <option value="en">English</option>
                        <option value="es">Español</option>
                        <option value="fr">Français</option>
                        <option value="de">Deutsch</option>
                        <option value="it">Italiano</option>
                        <option value="pt">Português</option>
                        <option value="ja">日本語</option>
                        <option value="zh">中文</option>
                        <option value="ko">한국어</option>
                        <option value="ru">Русский</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Sonidos</label>
                    <div class="flex items-center gap-2" style="height:32px">
                        <label class="toggle-switch">
                            <input type="checkbox" id="cfg-sounds">
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="text-xs text-white/40">Beep al iniciar/terminar</span>
                    </div>
                    <div class="flex items-center gap-2 mt-2">
                        <input type="range" id="cfg-beep-volume" min="1" max="10" step="1"
                               class="accent-purple-500" style="width:90px"
                               oninput="document.getElementById('cfg-beep-volume-label').textContent=this.value">
                        <span class="text-xs text-white/40">Volumen: <span id="cfg-beep-volume-label">2</span></span>
                    </div>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Guardar historial</label>
                    <div class="flex items-center gap-2" style="height:32px">
                        <label class="toggle-switch">
                            <input type="checkbox" id="cfg-save-history">
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="text-xs text-white/40">Guardar transcripciones</span>
                    </div>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Eliminar transcripciones después de (días)</label>
                    <select id="cfg-retention-days" class="cfg-select">
                        <option value="0">Nunca</option>
                        <option value="7">7 días</option>
                        <option value="30">30 días</option>
                        <option value="90">90 días</option>
                    </select>
                </div>
            </div>
            <div class="flex justify-end items-center mt-4 gap-3">
                <span id="cfg-saved" class="text-xs text-green-400" style="opacity:0;transition:opacity 0.3s">Guardado ✓</span>
                <button onclick="saveSettings()" class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50">Guardar</button>
            </div>
        </div>

        <!-- Table -->
        <div class="glass rounded-xl overflow-hidden">
            <table class="w-full">
                <thead>
                    <tr class="text-white/40 text-xs uppercase tracking-wider border-b border-white/5">
                        <th class="py-3 px-2 text-center w-10">
                            <input type="checkbox" id="select-all" onclick="toggleSelectAll(this)" class="accent-purple-500 cursor-pointer">
                        </th>
                        <th class="py-3 px-4 text-left w-36">Hora</th>
                        <th class="py-3 px-4 text-left">Transcripcion</th>
                        <th class="py-3 px-4 text-right w-20">Dur.</th>
                        <th class="py-3 px-4 text-center w-32"></th>
                    </tr>
                </thead>
                <tbody id="tbody"></tbody>
            </table>
            <div id="empty" class="hidden text-center py-12 text-white/20 text-sm">
                No hay transcripciones aun
            </div>
        </div>

        <!-- Footer -->
        <div class="mt-4 text-center text-white/15 text-xs">
            Vflow &middot; Ctrl+Shift para grabar &middot; Groq Whisper
        </div>
    </div>

    <!-- Selection bar -->
    <div class="selection-bar" id="selection-bar">
        <span class="count" id="sel-count">0 seleccionados</span>
        <button class="del-btn" onclick="deleteSelected()">Eliminar seleccionados</button>
        <button class="cancel-btn" onclick="clearSelection()">Cancelar</button>
    </div>

    <script>
        let allData = [];
        let renderedData = [];
        let editingId = null;
        let selectedIds = new Set();
        let expandedIds = new Set();
        let anchorIndex = null;

        async function loadData() {
            const res = await fetch('/api/transcriptions');
            allData = await res.json();
            // Remove selected IDs that no longer exist
            const existingIds = new Set(allData.map(t => t.id));
            selectedIds = new Set([...selectedIds].filter(id => existingIds.has(id)));
            renderTable(allData);
        }

        function renderTable(data) {
            renderedData = data;
            const tbody = document.getElementById('tbody');
            const empty = document.getElementById('empty');
            const badge = document.getElementById('count-badge');
            badge.textContent = data.length + ' total';

            if (data.length === 0) {
                tbody.innerHTML = '';
                empty.classList.remove('hidden');
                updateSelectionBar();
                return;
            }
            empty.classList.add('hidden');

            tbody.innerHTML = data.map((t, i) => {
                const date = new Date(t.created_at + 'Z');
                const time = date.toLocaleString('es-MX', {
                    month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit', second: '2-digit'
                });
                const dur = t.duration_seconds ? t.duration_seconds.toFixed(1) + 's' : '-';
                const isEditing = editingId === t.id;
                const checked = selectedIds.has(t.id) ? 'checked' : '';
                const rowClass = selectedIds.has(t.id) ? 'selected-row' : '';
                const textCell = isEditing
                    ? `<textarea class="edit-area" id="edit-${t.id}">${escapeHtml(t.text)}</textarea>
                       <div class="flex gap-2 mt-1">
                           <button onclick="event.stopPropagation(); saveEdit(${t.id})"
                               class="text-xs px-2 py-1 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50">Guardar</button>
                           <button onclick="event.stopPropagation(); cancelEdit()"
                               class="text-xs px-2 py-1 rounded text-white/40 hover:text-white/60 hover:bg-white/5">Cancelar</button>
                       </div>`
                    : `<div class="text-preview" id="text-${i}">${escapeHtml(t.text)}</div>`;
                return `
                <tr class="row-hover border-b border-white/[0.03] cursor-pointer ${rowClass}" data-id="${t.id}" onclick="handleRowClick(event, ${i})">
                    <td class="py-3 px-2 text-center align-top">
                        <input type="checkbox" class="row-check accent-purple-500 cursor-pointer" data-id="${t.id}"
                            ${checked} onclick="event.stopPropagation(); handleRowSelect(event, ${i})">
                    </td>
                    <td class="py-3 px-4 text-white/30 text-xs whitespace-nowrap align-top">${time}</td>
                    <td class="py-3 px-4 text-white/80 text-sm align-top">${textCell}</td>
                    <td class="py-3 px-4 text-white/20 text-xs text-right align-top">${dur}</td>
                    <td class="py-3 px-4 text-center align-top whitespace-nowrap">
                        <button onclick="event.stopPropagation(); copyText(${i}, this)"
                            class="text-white/20 hover:text-white/60 text-xs px-1.5 py-1 rounded hover:bg-white/5"
                            title="Copiar">Copiar</button>
                        <button onclick="event.stopPropagation(); startEdit(${t.id})"
                            class="text-white/20 hover:text-white/60 text-xs px-1.5 py-1 rounded hover:bg-white/5 ml-0.5"
                            title="Editar">&#9998;</button>
                        <button onclick="event.stopPropagation(); deleteSingle(${t.id}, this)"
                            class="text-white/20 hover:text-red-400 text-xs px-1.5 py-1 rounded hover:bg-red-500/10 ml-0.5"
                            title="Eliminar">&#10005;</button>
                    </td>
                </tr>`;
            }).join('');

            syncCheckboxUI();

            // Restore expanded state after re-render
            expandedIds.forEach(id => {
                const row = tbody.querySelector(`tr[data-id="${id}"]`);
                if (row) {
                    const preview = row.querySelector('.text-preview');
                    if (preview) preview.classList.add('expanded');
                }
            });
        }

        function toggleExpand(row) {
            const preview = row.querySelector('.text-preview');
            const id = parseInt(row.dataset.id);
            if (preview) {
                preview.classList.toggle('expanded');
                if (preview.classList.contains('expanded')) {
                    expandedIds.add(id);
                } else {
                    expandedIds.delete(id);
                }
            }
        }

        function copyText(index, btn) {
            navigator.clipboard.writeText(renderedData[index].text);
            const row = btn.closest('tr');
            row.classList.add('copied');
            btn.textContent = 'OK';
            setTimeout(() => { btn.textContent = 'Copiar'; row.classList.remove('copied'); }, 1000);
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // --- Edit ---
        function startEdit(id) {
            editingId = id;
            renderTable(allData);
            setTimeout(() => {
                const ta = document.getElementById('edit-' + id);
                if (ta) { ta.focus(); ta.selectionStart = ta.value.length; }
            }, 50);
        }

        function cancelEdit() {
            editingId = null;
            renderTable(allData);
        }

        async function saveEdit(id) {
            const ta = document.getElementById('edit-' + id);
            if (!ta) return;
            const newText = ta.value.trim();
            if (!newText) return;
            await fetch('/api/transcriptions/' + id, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text: newText})
            });
            editingId = null;
            loadData();
        }

        // --- Delete single ---
        async function deleteSingle(id, btn) {
            if (!confirm('¿Eliminar esta transcripcion?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            const row = btn.closest('tr');
            row.classList.add('deleted');
            setTimeout(async () => {
                await fetch('/api/transcriptions/' + id, {method: 'DELETE'});
                loadData();
            }, 350);
        }

        // --- Bulk delete ---
        async function bulkDelete(range, label) {
            document.getElementById('cleanup-dropdown').classList.remove('open');
            const labels = {day: 'las transcripciones de hoy', week: 'las transcripciones de la ultima semana',
                month: 'las transcripciones del ultimo mes', all: 'TODAS las transcripciones'};
            const desc = labels[range] || range;
            if (!confirm('¿Eliminar ' + desc + '?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            let url = '/api/transcriptions?range=' + range;
            if (range === 'day' && label === 'hoy') {
                url += '&date=' + new Date().toISOString().slice(0,10);
            }
            await fetch(url, {method: 'DELETE'});
            loadData();
        }

        async function deleteByDate() {
            document.getElementById('cleanup-dropdown').classList.remove('open');
            const dateStr = prompt('Ingresa la fecha (YYYY-MM-DD):');
            if (!dateStr || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(dateStr)) return;
            if (!confirm('¿Eliminar las transcripciones del ' + dateStr + '?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            await fetch('/api/transcriptions?range=day&date=' + dateStr, {method: 'DELETE'});
            loadData();
        }

        // --- Selection logic ---
        function handleRowClick(e, index) {
            if (e.target.closest('button, textarea, .edit-area')) return;
            if (e.target.classList.contains('row-check')) return;
            if (e.ctrlKey || e.shiftKey || e.metaKey) {
                e.preventDefault();
                handleRowSelect(e, index);
            } else {
                toggleExpand(e.currentTarget);
            }
        }

        function handleRowSelect(e, index) {
            const id = renderedData[index].id;

            if (e.ctrlKey && e.shiftKey) {
                if (anchorIndex !== null) {
                    const [start, end] = [Math.min(anchorIndex, index), Math.max(anchorIndex, index)];
                    for (let i = start; i <= end; i++) selectedIds.add(renderedData[i].id);
                } else {
                    selectedIds.add(id);
                    anchorIndex = index;
                }
            } else if (e.shiftKey) {
                selectedIds.clear();
                if (anchorIndex !== null) {
                    const [start, end] = [Math.min(anchorIndex, index), Math.max(anchorIndex, index)];
                    for (let i = start; i <= end; i++) selectedIds.add(renderedData[i].id);
                } else {
                    selectedIds.add(id);
                    anchorIndex = index;
                }
            } else if (e.ctrlKey || e.metaKey) {
                if (selectedIds.has(id)) selectedIds.delete(id);
                else selectedIds.add(id);
                anchorIndex = index;
            } else {
                selectedIds.clear();
                selectedIds.add(id);
                anchorIndex = index;
            }

            syncCheckboxUI();
        }

        function syncCheckboxUI() {
            const visibleChecks = document.querySelectorAll('.row-check');
            visibleChecks.forEach(c => {
                const id = parseInt(c.dataset.id);
                c.checked = selectedIds.has(id);
                c.closest('tr').classList.toggle('selected-row', c.checked);
            });
            const selectAll = document.getElementById('select-all');
            const allChecked = visibleChecks.length > 0 && [...visibleChecks].every(c => c.checked);
            const someChecked = [...visibleChecks].some(c => c.checked);
            selectAll.checked = allChecked;
            selectAll.indeterminate = !allChecked && someChecked;
            updateSelectionBar();
        }

        function toggleSelectAll(cb) {
            renderedData.forEach(t => {
                if (cb.checked) selectedIds.add(t.id);
                else selectedIds.delete(t.id);
            });
            anchorIndex = null;
            syncCheckboxUI();
        }

        function updateSelectionBar() {
            const bar = document.getElementById('selection-bar');
            const count = document.getElementById('sel-count');
            if (selectedIds.size > 0) {
                bar.classList.add('visible');
                count.textContent = selectedIds.size + ' seleccionado' + (selectedIds.size > 1 ? 's' : '');
            } else {
                bar.classList.remove('visible');
            }
        }

        function clearSelection() {
            selectedIds.clear();
            anchorIndex = null;
            syncCheckboxUI();
        }

        async function deleteSelected() {
            const n = selectedIds.size;
            if (!confirm('¿Eliminar ' + n + ' transcripcion' + (n > 1 ? 'es' : '') + ' seleccionada' + (n > 1 ? 's' : '') + '?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            await fetch('/api/transcriptions/delete-batch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ids: [...selectedIds]})
            });
            selectedIds.clear();
            loadData();
        }

        // Close dropdown on outside click
        document.addEventListener('click', (e) => {
            const dd = document.getElementById('cleanup-dropdown');
            if (dd && !dd.contains(e.target)) dd.classList.remove('open');
        });

        // Search
        document.getElementById('search').addEventListener('input', (e) => {
            const q = e.target.value.toLowerCase();
            if (!q) { renderTable(allData); return; }
            renderTable(allData.filter(t => t.text.toLowerCase().includes(q)));
        });

        // Collapse all expanded rows on Escape
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                expandedIds.clear();
                document.querySelectorAll('.text-preview.expanded').forEach(el => {
                    el.classList.remove('expanded');
                });
            }
        });

        // Auto-refresh every 5 seconds
        loadData();
        setInterval(loadData, 5000);

        // --- Settings panel ---
        async function toggleSettings() {
            const panel = document.getElementById('settings-panel');
            if (panel.classList.contains('hidden')) {
                panel.classList.remove('hidden');
                await loadSettings();
            } else {
                panel.classList.add('hidden');
            }
        }

        async function loadSettings() {
            const [settings, mics] = await Promise.all([
                fetch('/api/settings').then(r => r.json()),
                fetch('/api/microphones').then(r => r.json()),
            ]);
            document.getElementById('cfg-language').value = settings.language || 'es';
            document.getElementById('cfg-translate-target').value = settings.translate_target || 'en';
            document.getElementById('cfg-sounds').checked = settings.sounds_enabled !== false;
            const vol = settings.beep_volume || 2;
            document.getElementById('cfg-beep-volume').value = vol;
            document.getElementById('cfg-beep-volume-label').textContent = vol;
            document.getElementById('cfg-save-history').checked = settings.save_history !== false;
            const retDays = settings.retention_days || 0;
            const retSelect = document.getElementById('cfg-retention-days');
            const retOpt = retSelect.querySelector('option[value="' + retDays + '"]');
            retSelect.value = retOpt ? String(retDays) : '0';
            const micSelect = document.getElementById('cfg-microphone');
            micSelect.innerHTML = '<option value="">Sistema por defecto</option>';
            mics.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m.name;
                opt.textContent = m.name;
                if (m.name === settings.device_name) opt.selected = true;
                micSelect.appendChild(opt);
            });
        }

        async function saveSettings() {
            const data = {
                language: document.getElementById('cfg-language').value,
                translate_target: document.getElementById('cfg-translate-target').value,
                device_name: document.getElementById('cfg-microphone').value,
                sounds_enabled: document.getElementById('cfg-sounds').checked ? 'true' : 'false',
                beep_volume: document.getElementById('cfg-beep-volume').value,
                save_history: document.getElementById('cfg-save-history').checked ? 'true' : 'false',
                retention_days: document.getElementById('cfg-retention-days').value,
            };
            await fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data),
            });
            const saved = document.getElementById('cfg-saved');
            saved.style.opacity = '1';
            setTimeout(() => { saved.style.opacity = '0'; }, 2000);
        }
    </script>
</body>
</html>
"""

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _is_local_url(url: str) -> bool:
    """Devuelve True solo si la URL apunta exactamente a un host local.

    Parsea el hostname con urlparse para evitar bypasses por prefijo como
    http://localhost.evil.com (hostname sería "localhost.evil.com", no "localhost").
    Cualquier puerto local es válido; solo el hostname es verificado.
    """
    host = urlparse(url).hostname
    return host in _LOCAL_HOSTNAMES


@app.before_request
def _csrf_check():
    """Block cross-origin requests to mutating endpoints."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    # Permitir requests sin Origin ni Referer (mismo origen, curl, etc.)
    if not origin and not referer:
        return
    if origin and not _is_local_url(origin):
        return jsonify({"error": "CSRF: origin not allowed"}), 403
    if referer and not origin:
        if not _is_local_url(referer):
            return jsonify({"error": "CSRF: referer not allowed"}), 403


@app.route("/")
def index():
    """Sirve la página principal del dashboard de transcripciones."""
    return render_template_string(HTML_TEMPLATE)


@app.route("/logo")
def logo():
    """Sirve el logo de la app para el dashboard."""
    from config import LOGO_PATH
    return send_file(LOGO_PATH, mimetype="image/png")


@app.route("/api/transcriptions")
def get_transcriptions():
    """Retorna las últimas 200 transcripciones en formato JSON."""
    return jsonify(_db.get_recent(limit=200))


@app.route("/api/transcriptions/<int:tid>", methods=["DELETE"])
def delete_transcription(tid):
    """Elimina una transcripción individual por su ID."""
    deleted = _db.delete_by_id(tid)
    return jsonify({"deleted": deleted})


@app.route("/api/transcriptions", methods=["DELETE"])
def delete_transcriptions_bulk():
    """Elimina transcripciones en lote por rango: day, week, month o all."""
    range_type = request.args.get("range", "")
    if range_type == "all":
        deleted = _db.delete_all()
    elif range_type == "day":
        date = request.args.get("date", "")
        if not date or not _DATE_RE.match(date):
            return jsonify({"error": "date parameter required (YYYY-MM-DD)"}), 400
        deleted = _db.delete_by_date(date)
    elif range_type == "week":
        since = (datetime.utcnow() - timedelta(weeks=1)).strftime("%Y-%m-%d")
        deleted = _db.delete_since(since)
    elif range_type == "month":
        since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        deleted = _db.delete_since(since)
    else:
        return jsonify({"error": "invalid range"}), 400
    return jsonify({"deleted": deleted})


@app.route("/api/transcriptions/delete-batch", methods=["POST"])
def delete_transcriptions_batch():
    """Elimina múltiples transcripciones por una lista de IDs en el body JSON."""
    data = request.get_json()
    if not data or "ids" not in data:
        return jsonify({"error": "ids field required"}), 400
    try:
        ids = [int(i) for i in data["ids"]]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400
    deleted = _db.delete_by_ids(ids)
    return jsonify({"deleted": deleted})


@app.route("/api/transcriptions/<int:tid>", methods=["PUT"])
def update_transcription(tid):
    """Actualiza el texto de una transcripción existente."""
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "text field required"}), 400
    updated = _db.update_text(tid, data["text"])
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


def _set_env_key(key: str, value: str):
    """Write key=value to .env and update the running process environment."""
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    set_key(_ENV_PATH, key, value)
    os.environ[key] = value


@app.route("/api/settings")
def get_settings():
    """Devuelve la configuración actual (idioma, micrófono, sonidos)."""
    return jsonify({
        "language": os.getenv("WHISPER_LANGUAGE", "es"),
        "translate_target": os.getenv("TRANSLATE_TARGET_LANG", "en"),
        "device_name": os.getenv("AUDIO_DEVICE_NAME", ""),
        "sounds_enabled": os.getenv("SOUNDS_ENABLED", "true") == "true",
        "beep_volume": int(os.getenv("BEEP_VOLUME_STEPS", "2")),
        "save_history": os.getenv("SAVE_HISTORY", "true").lower() == "true",
        "retention_days": int(os.getenv("HISTORY_RETENTION_DAYS", "0") or 0),
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Guarda configuración en .env y actualiza el proceso en ejecución."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400
    allowed = {
        "language": "WHISPER_LANGUAGE",
        "translate_target": "TRANSLATE_TARGET_LANG",
        "device_name": "AUDIO_DEVICE_NAME",
        "sounds_enabled": "SOUNDS_ENABLED",
        "beep_volume": "BEEP_VOLUME_STEPS",
        "save_history": "SAVE_HISTORY",
        "retention_days": "HISTORY_RETENTION_DAYS",
    }
    for field, env_key in allowed.items():
        if field in data:
            _set_env_key(env_key, str(data[field]).strip())
    return jsonify({"ok": True})


@app.route("/api/microphones")
def get_microphones():
    """Lista los dispositivos de entrada de audio (excluye salidas/speakers)."""
    import sounddevice as sd
    mics = [
        {"index": i, "name": dev["name"]}
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] > 0 and dev["max_output_channels"] == 0
    ]
    return jsonify(mics)


def _find_free_port(start: int = 5678, attempts: int = 50) -> int:
    """Find an available port starting from `start`."""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{start + attempts - 1}")


def start_web_server(port: int = None) -> int:
    """Start Flask in a daemon thread so it doesn't block the Qt event loop."""
    if port is None:
        port = _find_free_port()
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return port
