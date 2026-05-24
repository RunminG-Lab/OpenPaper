(function () {
    const MISSING_TEXT = '论文中未明确说明';

    function escapeHtml(value = '') {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function textValue(value, fallback = MISSING_TEXT) {
        if (typeof value !== 'string') return fallback;
        const cleaned = value.trim();
        return cleaned || fallback;
    }

    function cleanFigureSummary(value, fallback = MISSING_TEXT) {
        const raw = textValue(value, '');
        if (!raw) return fallback;
        const lines = raw.split(/\n+/).map(line => {
            const cleaned = line
                .replace(/^\s*(展示什么|看什么|先看哪里|视觉元素|支撑结论|它说明了什么|为什么重要|方法\/实验关系|容易误读)\s*[:：]\s*/, '')
                .trim();
            return cleaned && cleaned !== MISSING_TEXT ? cleaned : '';
        }).filter(Boolean);
        return lines.join(' ') || fallback;
    }

    function listValue(value, fallback = MISSING_TEXT) {
        if (!Array.isArray(value)) return [fallback];
        const items = value.map(item => {
            if (typeof item === 'string') return item.trim();
            if (item && typeof item === 'object') return textValue(item.text || item.summary || item.name, '');
            return '';
        }).filter(Boolean);
        return items.length ? items : [fallback];
    }

    function pageRefs(value) {
        if (!Array.isArray(value)) return [];
        const refs = [];
        value.forEach(item => {
            const page = Number.parseInt(item, 10);
            if (Number.isFinite(page) && page > 0 && !refs.includes(page)) refs.push(page);
        });
        return refs;
    }

    function formatSpeedReadTime(isoStr) {
        if (!isoStr) return '时间未知';
        const date = new Date(isoStr);
        if (Number.isNaN(date.getTime())) return isoStr;
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
    }

    function renderTextBlock(text) {
        return escapeHtml(textValue(text)).replace(/\n/g, '<br>');
    }

    function renderBulletList(items) {
        const list = listValue(items);
        return `<ul class="qr-bullets">${list.map(item => `<li>${renderTextBlock(item)}</li>`).join('')}</ul>`;
    }

    function normalizeComparedMethods(value) {
        if (!Array.isArray(value)) return [];
        return value.map(item => {
            if (typeof item === 'string') {
                return { name: textValue(item), role: 'other', description: MISSING_TEXT, result: MISSING_TEXT, page_refs: [] };
            }
            if (!item || typeof item !== 'object') return null;
            const role = ['proposed', 'baseline', 'ablation', 'variant', 'other'].includes(String(item.role || '').toLowerCase())
                ? String(item.role).toLowerCase()
                : 'other';
            return {
                name: textValue(item.name),
                role,
                description: textValue(item.description),
                result: textValue(item.result),
                page_refs: pageRefs(item.page_refs),
            };
        }).filter(Boolean);
    }

    function normalizeDatasets(value) {
        if (!Array.isArray(value)) return [];
        return value.map(item => {
            if (typeof item === 'string') {
                return { name: textValue(item), task: MISSING_TEXT, scale: MISSING_TEXT, split: MISSING_TEXT, notes: MISSING_TEXT, page_refs: [] };
            }
            if (!item || typeof item !== 'object') return null;
            return {
                name: textValue(item.name),
                task: textValue(item.task),
                scale: textValue(item.scale),
                split: textValue(item.split),
                notes: textValue(item.notes),
                page_refs: pageRefs(item.page_refs),
            };
        }).filter(Boolean);
    }

    function normalizeMetrics(value) {
        if (!Array.isArray(value)) return [];
        return value.map(item => {
            if (typeof item === 'string') {
                return { name: textValue(item), meaning: MISSING_TEXT, why_used: MISSING_TEXT, limitation: MISSING_TEXT, page_refs: [] };
            }
            if (!item || typeof item !== 'object') return null;
            return {
                name: textValue(item.name),
                meaning: textValue(item.meaning),
                why_used: textValue(item.why_used),
                limitation: textValue(item.limitation),
                page_refs: pageRefs(item.page_refs),
            };
        }).filter(Boolean);
    }

    function normalizeMainResults(value) {
        if (!Array.isArray(value)) return [];
        return value.map(item => {
            if (typeof item === 'string') {
                return { claim: textValue(item), evidence: MISSING_TEXT, page_refs: [] };
            }
            if (!item || typeof item !== 'object') return null;
            return {
                claim: textValue(item.claim || item.result || item.summary),
                evidence: textValue(item.evidence || item.detail),
                page_refs: pageRefs(item.page_refs),
            };
        }).filter(Boolean);
    }

    function normalizeExperimentRead(experimentRead) {
        if (typeof experimentRead === 'string') {
            return {
                summary: textValue(experimentRead),
                compared_methods: [],
                datasets: [],
                metrics: [],
                main_results: [],
                reproduction_difficulty: 'unknown',
                reproduction_notes: [MISSING_TEXT],
                workload_estimate: MISSING_TEXT,
                evaluation_design_notes: [MISSING_TEXT],
                missing_details: ['旧格式缓存未提供结构化实验字段'],
            };
        }
        const source = experimentRead && typeof experimentRead === 'object' ? experimentRead : {};
        const difficulty = ['low', 'medium', 'high', 'unknown'].includes(String(source.reproduction_difficulty || '').toLowerCase())
            ? String(source.reproduction_difficulty).toLowerCase()
            : 'unknown';
        return {
            summary: textValue(source.summary),
            compared_methods: normalizeComparedMethods(source.compared_methods),
            datasets: normalizeDatasets(source.datasets),
            metrics: normalizeMetrics(source.metrics),
            main_results: normalizeMainResults(source.main_results),
            reproduction_difficulty: difficulty,
            reproduction_notes: listValue(source.reproduction_notes),
            workload_estimate: textValue(source.workload_estimate),
            evaluation_design_notes: listValue(source.evaluation_design_notes),
            missing_details: listValue(source.missing_details),
        };
    }

    function compactFigureSummary(item = {}, asset = {}) {
        const direct = cleanFigureSummary(item.summary || item.interpretation || item.analysis || item.description, '');
        if (direct) return direct;
        const parts = [
            item.what_it_shows || item.what_to_look,
            item.how_to_read,
            item.supports_claim || item.what_it_proves,
            item.relation_to_method_or_experiment || item.why_important,
            item.misread_risks,
        ].map(value => cleanFigureSummary(value, '')).filter(Boolean);
        if (parts.length) return [...new Set(parts)].join(' ');
        return cleanFigureSummary(asset.caption || asset.excerpt, MISSING_TEXT);
    }

    function normalizeFigureInsights(speedRead) {
        const content = speedRead && speedRead.content && typeof speedRead.content === 'object' ? speedRead.content : {};
        const candidates = Array.isArray(speedRead && speedRead.candidate_pages) ? speedRead.candidate_pages : [];
        const byId = new Map();
        const byPage = new Map();
        candidates.forEach(item => {
            if (!item || typeof item !== 'object') return;
            if (item.id) byId.set(String(item.id), item);
            const page = Number.parseInt(item.page, 10);
            if (Number.isFinite(page) && !byPage.has(page)) byPage.set(page, item);
        });

        let raw = Array.isArray(content.figure_insights) ? content.figure_insights : null;
        if (!raw && Array.isArray(content.core_figures)) raw = content.core_figures;
        if (!raw) raw = [];

        const usedCandidateIds = new Set();
        const normalized = raw.map((item, index) => {
            if (!item || typeof item !== 'object') return null;
            const rawId = item.figure_id || item.id || item.candidate_id || '';
            const page = Number.parseInt(item.page, 10);
            const asset = byId.get(String(rawId)) || candidates[index] || byPage.get(page) || {};
            const resolvedPage = Number.isFinite(page) ? page : Number.parseInt(asset.page, 10);
            const assetId = asset.id ? String(asset.id) : '';
            if (assetId) usedCandidateIds.add(assetId);
            const summary = compactFigureSummary(item, asset);
            return {
                id: String(rawId || asset.id || `fig_${String(index + 1).padStart(3, '0')}`),
                kind: asset.kind || item.kind || 'page_snapshot',
                label: textValue(item.label || asset.label, asset.label || '关键页面'),
                caption: textValue(item.caption || asset.caption, asset.caption || MISSING_TEXT),
                page: Number.isFinite(resolvedPage) ? resolvedPage : '',
                image_path: asset.image_path || item.image_path || '',
                confidence: asset.confidence || item.confidence || '',
                title: textValue(item.title || item.what_it_shows || asset.reason, '关键图表页'),
                summary,
                page_refs: pageRefs(item.page_refs || [resolvedPage]),
            };
        }).filter(Boolean);

        const fallbackItems = candidates
            .filter(asset => !asset.id || !usedCandidateIds.has(String(asset.id)))
            .map((asset, index) => ({
            id: String(asset.id || `fig_${String(index + 1).padStart(3, '0')}`),
            kind: asset.kind || 'page_snapshot',
            label: asset.label || '关键页面',
            caption: asset.caption || MISSING_TEXT,
            page: asset.page || '',
            image_path: asset.image_path || '',
            confidence: asset.confidence || '',
            title: asset.reason || asset.label || '关键图表页',
            summary: cleanFigureSummary(asset.caption || asset.excerpt, MISSING_TEXT),
            page_refs: pageRefs([asset.page]),
        }));
        const merged = normalized.concat(fallbackItems);
        return candidates.length ? merged.slice(0, candidates.length) : merged;
    }

    function normalizeSpeedReadContent(speedRead) {
        const content = speedRead && speedRead.content && typeof speedRead.content === 'object' ? speedRead.content : {};
        return {
            one_sentence: textValue(content.one_sentence),
            quick_takeaways: listValue(content.quick_takeaways),
            problem_and_motivation: textValue(content.problem_and_motivation),
            method_overview: textValue(content.method_overview),
            method_steps: listValue(content.method_steps),
            experiment_read: normalizeExperimentRead(content.experiment_read),
            contributions: listValue(content.contributions),
            limitations: listValue(content.limitations),
            figure_insights: normalizeFigureInsights(speedRead),
            deep_read_suggestions: listValue(content.deep_read_suggestions),
        };
    }

    function renderRefs(refs) {
        const pages = pageRefs(refs);
        return pages.length ? `<span class="qr-page-ref">Page ${pages.join(', ')}</span>` : '';
    }

    function renderEmptyLine(text = '论文中未明确说明') {
        return `<div class="qr-empty-line">${escapeHtml(text)}</div>`;
    }

    function renderRecordCards(items, renderer) {
        if (!Array.isArray(items) || !items.length) return renderEmptyLine();
        return `<div class="qr-record-grid">${items.map(renderer).join('')}</div>`;
    }

    function renderExperimentRead(experimentRead) {
        const data = normalizeExperimentRead(experimentRead);
        const difficultyText = { low: '低', medium: '中', high: '高', unknown: '未知' }[data.reproduction_difficulty] || '未知';
        return `
            <div class="qr-experiment-summary">${renderTextBlock(data.summary)}</div>
            <div class="qr-mini-metrics">
                <div><span>复现难度</span><strong>${escapeHtml(difficultyText)}</strong></div>
                <div><span>单人工作量</span><strong>${escapeHtml(data.workload_estimate)}</strong></div>
            </div>
            <section class="qr-subsection"><h3>对比方法 / Baseline / Ablation / Variant</h3>${renderRecordCards(data.compared_methods, item => `
                <article class="qr-record"><div class="qr-record-head"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.role)}</span></div>
                <p>${renderTextBlock(item.description)}</p><p><b>结果：</b>${renderTextBlock(item.result)}</p>${renderRefs(item.page_refs)}</article>`)}
            </section>
            <section class="qr-subsection"><h3>数据集与任务</h3>${renderRecordCards(data.datasets, item => `
                <article class="qr-record"><div class="qr-record-head"><strong>${escapeHtml(item.name)}</strong>${renderRefs(item.page_refs)}</div>
                <p><b>任务：</b>${renderTextBlock(item.task)}</p><p><b>规模：</b>${renderTextBlock(item.scale)}</p><p><b>划分：</b>${renderTextBlock(item.split)}</p><p><b>备注：</b>${renderTextBlock(item.notes)}</p></article>`)}
            </section>
            <section class="qr-subsection"><h3>指标设计</h3>${renderRecordCards(data.metrics, item => `
                <article class="qr-record"><div class="qr-record-head"><strong>${escapeHtml(item.name)}</strong>${renderRefs(item.page_refs)}</div>
                <p><b>含义：</b>${renderTextBlock(item.meaning)}</p><p><b>使用原因：</b>${renderTextBlock(item.why_used)}</p><p><b>局限：</b>${renderTextBlock(item.limitation)}</p></article>`)}
            </section>
            <section class="qr-subsection"><h3>主要结果</h3>${renderRecordCards(data.main_results, item => `
                <article class="qr-record"><p><b>结论：</b>${renderTextBlock(item.claim)}</p><p><b>证据：</b>${renderTextBlock(item.evidence)}</p>${renderRefs(item.page_refs)}</article>`)}
            </section>
            <div class="qr-two-col">
                <section class="qr-subsection"><h3>复现注意</h3>${renderBulletList(data.reproduction_notes)}</section>
                <section class="qr-subsection"><h3>实验设计充分性</h3>${renderBulletList(data.evaluation_design_notes)}</section>
            </div>
            <section class="qr-subsection"><h3>缺失细节</h3>${renderBulletList(data.missing_details)}</section>
        `;
    }

    function renderFigureInsights(figureInsights) {
        const items = Array.isArray(figureInsights) ? figureInsights : [];
        if (!items.length) return '<div class="qr-empty-line">当前速读没有图表解读。</div>';
        return `<div class="qr-figure-grid">${items.map(item => {
            const imageHtml = item.image_path
                ? `<img src="${escapeHtml(item.image_path)}" alt="${escapeHtml(item.label || 'figure')}" loading="lazy">`
                : '<div class="qr-image-placeholder">无截图</div>';
            const confidence = item.confidence ? ` · 置信度 ${escapeHtml(String(item.confidence))}` : '';
            return `
                <article class="qr-figure-card">
                    <div class="qr-figure-media">${imageHtml}</div>
                    <div class="qr-figure-body">
                        <div class="qr-chip">${escapeHtml(item.label || '关键页面')} · Page ${escapeHtml(String(item.page || '?'))}${confidence}</div>
                        <h3>${renderTextBlock(item.title)}</h3>
                        <p class="qr-figure-summary">${renderTextBlock(item.summary || item.what_it_shows || item.caption)}</p>
                    </div>
                </article>
            `;
        }).join('')}</div>`;
    }

    window.QuickReadingCommon = {
        MISSING_TEXT,
        escapeHtml,
        formatSpeedReadTime,
        renderTextBlock,
        renderBulletList,
        normalizeSpeedReadContent,
        normalizeExperimentRead,
        normalizeFigureInsights,
        renderExperimentRead,
        renderFigureInsights,
    };
    window.normalizeSpeedReadContent = normalizeSpeedReadContent;
    window.renderExperimentRead = renderExperimentRead;
    window.renderFigureInsights = renderFigureInsights;
})();
