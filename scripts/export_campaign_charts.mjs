#!/usr/bin/env node
/**
 * Export dashboard chart PNGs for all campaigns.
 *
 * Usage:
 *   node scripts/export_campaign_charts.mjs
 *   node scripts/export_campaign_charts.mjs --only connect-timeline-chart --only connect-timeline-full
 *   node scripts/export_campaign_charts.mjs --timeline --force
 */

import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const DB_PATH = path.join(ROOT, 'data', 'outbound_dashboard.db');
const CHARTS_DIR =
  process.env.OUTBOUND_CHARTS_DIR ||
  path.join(ROOT, 'exports', 'charts');
const DASHBOARD = process.env.OUTBOUND_DASHBOARD_URL || 'http://127.0.0.1:1880/dashboard';
const DATE = new Date().toISOString().slice(0, 10);
const DROPDOWN_ID = 'e8f9a0b1c2d34567';

const HEALTH_CHARTS = [
  { widgetId: '3b9d5f2a7c4e8106', slug: 'compliance-delay-bands', kind: 'chart' },
  { widgetId: '7f318936b0824540', slug: 'daily-call-volume', kind: 'chart' },
  { widgetId: 'chart_ca_dialdelay01', slug: 'daily-interaction-counts', kind: 'chart' },
  { widgetId: '9123ab58d2a46762', slug: 'daily-disposition-agent', kind: 'chart' },
  { widgetId: 'chart_ca_abandon01', slug: 'daily-disposition-abandon', kind: 'chart' },
];

const SANKEY = {
  widgetId: 'tpl_ca_sankey01',
  slug: 'contact-flow-sankey',
  kind: 'echarts',
  selector: '.contact-sankey-chart',
};

const TIMELINE = {
  widgetId: 'tpl_la_timeline01',
  slug: 'connect-timeline-chart',
  kind: 'html',
  selector: '.connect-timeline-boundary-map',
  fullSlug: 'connect-timeline-full',
};

const TIMELINE_SLUGS = [TIMELINE.slug, TIMELINE.fullSlug];

const ALL_SLUGS = [
  ...HEALTH_CHARTS.map((c) => c.slug),
  SANKEY.slug,
  ...TIMELINE_SLUGS,
];

function parseArgs(argv) {
  const only = [];
  let force = false;
  let timelineOnly = false;
  for (let i = 2; i < argv.length; i += 1) {
    if (argv[i] === '--force') {
      force = true;
    } else if (argv[i] === '--timeline') {
      timelineOnly = true;
    } else if (argv[i] === '--only') {
      only.push(argv[i + 1]);
      i += 1;
    }
  }
  const selectedSlugs = timelineOnly ? TIMELINE_SLUGS : only.length ? only : ALL_SLUGS;
  for (const slug of selectedSlugs) {
    if (!ALL_SLUGS.includes(slug)) {
      throw new Error(`Unknown chart slug: ${slug}`);
    }
  }
  return { selectedSlugs, force };
}

function loadCampaigns() {
  const out = spawnSync('sqlite3', ['-separator', '|', DB_PATH, 'SELECT id, name FROM campaigns ORDER BY name'], {
    encoding: 'utf8',
  });
  if (out.status !== 0) {
    throw new Error(out.stderr || 'Failed to read campaigns from SQLite');
  }
  return out.stdout
    .trim()
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const idx = line.indexOf('|');
      return { id: line.slice(0, idx), name: line.slice(idx + 1) };
    });
}

function existingSlugs(campaignId) {
  if (!fs.existsSync(CHARTS_DIR)) return new Set();
  const found = new Set();
  for (const file of fs.readdirSync(CHARTS_DIR)) {
    if (!file.startsWith(`${campaignId}_`) || !file.endsWith('.png')) continue;
    for (const slug of ALL_SLUGS) {
      if (file.includes(`_${slug}_`) || file.includes(`_${slug}.png`)) {
        found.add(slug);
      }
    }
  }
  return found;
}

function saveDataUrl(dataUrl, filepath) {
  const base64 = dataUrl.replace(/^data:image\/png;base64,/, '');
  fs.writeFileSync(filepath, Buffer.from(base64, 'base64'));
}

async function waitForWidgetChart(page, widgetId, selector, timeout = 120000) {
  await page.waitForFunction(
    ({ widgetId, selector }) => {
      const widget = document.getElementById(`nrdb-ui-widget-${widgetId}`);
      if (!widget) return false;
      const canvas = widget.querySelector('canvas');
      if (canvas && canvas.width > 0 && canvas.height > 0) return true;
      const el = selector ? widget.querySelector(selector) : null;
      if (el && window.echarts?.getInstanceByDom(el)) return true;
      if (el && el.offsetHeight > 0 && !widget.querySelector('.connect-timeline-empty')) return true;
      return false;
    },
    { widgetId, selector: selector || null },
    { timeout }
  );
}

async function exportHtmlSelectorPng(page, widgetId, selector) {
  return page.evaluate(async ({ widgetId, selector }) => {
    const widget = document.getElementById(`nrdb-ui-widget-${widgetId}`);
    if (!widget) return { ok: false, reason: 'widget-not-found' };
    const el = widget.querySelector(selector);
    if (!el) return { ok: false, reason: 'selector-not-found' };
    if (!window.html2canvas) return { ok: false, reason: 'html2canvas-missing' };
    const canvas = await window.html2canvas(el, {
      backgroundColor: '#ffffff',
      scale: 2,
      useCORS: true,
    });
    return { ok: true, dataUrl: canvas.toDataURL('image/png') };
  }, { widgetId, selector });
}

async function exportWidgetPng(page, chart) {
  if (chart.kind === 'html') {
    return exportHtmlSelectorPng(page, chart.widgetId, chart.selector);
  }
  return page.evaluate(
    ({ widgetId, kind, selector }) => {
      const widget = document.getElementById(`nrdb-ui-widget-${widgetId}`);
      if (!widget) return { ok: false, reason: 'widget-not-found' };
      if (kind === 'echarts' && window.echarts) {
        const el = widget.querySelector(selector);
        const inst = el && window.echarts.getInstanceByDom(el);
        if (!inst) return { ok: false, reason: 'echarts-not-ready' };
        return {
          ok: true,
          dataUrl: inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' }),
        };
      }
      const canvas = widget.querySelector('canvas');
      if (!canvas) return { ok: false, reason: 'canvas-not-found' };
      return { ok: true, dataUrl: canvas.toDataURL('image/png') };
    },
    { widgetId: chart.widgetId, kind: chart.kind, selector: chart.selector || null }
  );
}

async function ensureHtml2Canvas(page) {
  await page.evaluate(async () => {
    if (window.html2canvas) return true;
    await new Promise((resolve, reject) => {
      const existing = document.querySelector('script[data-export-html2canvas="1"]');
      if (existing) {
        existing.addEventListener('load', () => resolve(true));
        existing.addEventListener('error', () => reject(new Error('html2canvas load failed')));
        return;
      }
      const script = document.createElement('script');
      script.src = 'https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js';
      script.dataset.exportHtml2canvas = '1';
      script.onload = () => resolve(true);
      script.onerror = () => reject(new Error('html2canvas load failed'));
      document.head.appendChild(script);
    });
    return Boolean(window.html2canvas);
  });
}

async function exportTimelineFull(page) {
  await ensureHtml2Canvas(page);
  return page.evaluate(async () => {
    const widget = document.getElementById('nrdb-ui-widget-tpl_la_timeline01');
    if (!widget) return { ok: false, reason: 'widget-not-found' };
    const empty = widget.querySelector('.connect-timeline-empty');
    if (empty && empty.offsetParent !== null) {
      return { ok: false, reason: 'no-timeline-data' };
    }
    const root =
      widget.querySelector('.connect-timeline-customer-content') ||
      widget.querySelector('.connect-timeline-customer') ||
      widget.querySelector('.connect-timeline-wrap');
    if (!root) return { ok: false, reason: 'timeline-wrap-not-found' };
    const toolbar = root.querySelector('.chart-export-toolbar');
    const prevVisibility = toolbar?.style.visibility;
    if (toolbar) toolbar.style.visibility = 'hidden';
    if (!window.html2canvas) return { ok: false, reason: 'html2canvas-missing' };
    try {
      const canvas = await window.html2canvas(root, {
        backgroundColor: '#ffffff',
        scale: 2,
        useCORS: true,
        logging: false,
      });
      return { ok: true, dataUrl: canvas.toDataURL('image/png') };
    } finally {
      if (toolbar) toolbar.style.visibility = prevVisibility || '';
    }
  });
}

async function selectCampaign(page, campaign) {
  await page.goto(`${DASHBOARD}/overview`, { waitUntil: 'domcontentloaded' });
  const dropdown = page.locator(`#nrdb-ui-widget-${DROPDOWN_ID}`);
  await dropdown.waitFor({ state: 'visible', timeout: 30000 });
  await dropdown.locator('.v-field').click();
  const option = page.locator('.v-overlay-container .v-list-item').filter({ hasText: campaign.name });
  await option.first().click();
  await page.waitForFunction(
    (name) => document.querySelector('.campaign-status-name')?.textContent?.includes(name),
    campaign.name,
    { timeout: 300000 }
  );
  await page.waitForTimeout(5000);
}

async function clickTab(page, tabName) {
  const tab = page.getByRole('tab', { name: tabName, exact: true });
  await tab.click();
  await page.waitForTimeout(2000);
}

async function exportCampaign(page, campaign, selectedSlugs, force) {
  const have = force ? new Set() : existingSlugs(campaign.id);
  const need = selectedSlugs.filter((slug) => !have.has(slug));
  if (!need.length) {
    console.log(`  skip ${campaign.id} — already complete`);
    return { campaign: campaign.id, exported: [], skipped: selectedSlugs };
  }

  console.log(`  exporting ${campaign.id} — missing: ${need.join(', ')}`);
  await selectCampaign(page, campaign);
  const exported = [];
  const failed = [];

  const healthNeeded = HEALTH_CHARTS.filter((c) => need.includes(c.slug));
  if (healthNeeded.length) {
    await clickTab(page, 'Campaign Health');
    for (const chart of healthNeeded) {
      const outfile = path.join(CHARTS_DIR, `${campaign.id}_${chart.slug}_${DATE}.png`);
      try {
        await waitForWidgetChart(page, chart.widgetId, null, 120000);
        const result = await exportWidgetPng(page, chart);
        if (!result.ok) throw new Error(result.reason);
        saveDataUrl(result.dataUrl, outfile);
        exported.push(chart.slug);
        console.log(`    saved ${path.basename(outfile)}`);
      } catch (err) {
        failed.push({ slug: chart.slug, error: String(err.message || err) });
        console.warn(`    failed ${chart.slug}: ${err.message || err}`);
      }
    }
  }

  if (need.includes(SANKEY.slug)) {
    await page.goto(`${DASHBOARD}/contact-analysis`, { waitUntil: 'domcontentloaded' });
    await clickTab(page, 'Contact Flow');
    const outfile = path.join(CHARTS_DIR, `${campaign.id}_${SANKEY.slug}_${DATE}.png`);
    try {
      await waitForWidgetChart(page, SANKEY.widgetId, SANKEY.selector, 180000);
      await page.waitForTimeout(1500);
      const result = await exportWidgetPng(page, SANKEY);
      if (!result.ok) throw new Error(result.reason);
      saveDataUrl(result.dataUrl, outfile);
      exported.push(SANKEY.slug);
      console.log(`    saved ${path.basename(outfile)}`);
    } catch (err) {
      failed.push({ slug: SANKEY.slug, error: String(err.message || err) });
      console.warn(`    failed ${SANKEY.slug}: ${err.message || err}`);
    }
  }

  const timelineNeeded = need.includes(TIMELINE.slug) || need.includes(TIMELINE.fullSlug);
  if (timelineNeeded) {
    await page.goto(`${DASHBOARD}/latency-analysis`, { waitUntil: 'domcontentloaded' });
    await clickTab(page, 'Connect Timeline');
    await page.waitForTimeout(1500);

    if (need.includes(TIMELINE.slug)) {
      const outfile = path.join(CHARTS_DIR, `${campaign.id}_${TIMELINE.slug}_${DATE}.png`);
      try {
        await waitForWidgetChart(page, TIMELINE.widgetId, TIMELINE.selector, 120000);
        const result = await exportWidgetPng(page, TIMELINE);
        if (!result.ok) throw new Error(result.reason);
        saveDataUrl(result.dataUrl, outfile);
        exported.push(TIMELINE.slug);
        console.log(`    saved ${path.basename(outfile)}`);
      } catch (err) {
        failed.push({ slug: TIMELINE.slug, error: String(err.message || err) });
        console.warn(`    failed ${TIMELINE.slug}: ${err.message || err}`);
      }
    }

    if (need.includes(TIMELINE.fullSlug)) {
      const outfile = path.join(CHARTS_DIR, `${campaign.id}_${TIMELINE.fullSlug}_${DATE}.png`);
      try {
        if (!need.includes(TIMELINE.slug)) {
          await waitForWidgetChart(page, TIMELINE.widgetId, TIMELINE.selector, 120000);
        }
        await ensureHtml2Canvas(page);
        const result = await exportTimelineFull(page);
        if (!result.ok) throw new Error(result.reason);
        saveDataUrl(result.dataUrl, outfile);
        exported.push(TIMELINE.fullSlug);
        console.log(`    saved ${path.basename(outfile)}`);
      } catch (err) {
        failed.push({ slug: TIMELINE.fullSlug, error: String(err.message || err) });
        console.warn(`    failed ${TIMELINE.fullSlug}: ${err.message || err}`);
      }
    }
  }

  return { campaign: campaign.id, exported, failed };
}

async function main() {
  const { selectedSlugs, force } = parseArgs(process.argv);
  fs.mkdirSync(CHARTS_DIR, { recursive: true });
  const campaigns = loadCampaigns();
  console.log(`Charts dir: ${CHARTS_DIR}`);
  console.log(`Campaigns: ${campaigns.length}`);
  console.log(`Charts: ${selectedSlugs.join(', ')}${force ? ' (force re-export)' : ''}`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1200 } });
  const page = await context.newPage();
  page.setDefaultNavigationTimeout(180000);
  page.setDefaultTimeout(180000);

  const summary = [];
  try {
    for (const campaign of campaigns) {
      console.log(`\n${campaign.name} (${campaign.id})`);
      try {
        summary.push(await exportCampaign(page, campaign, selectedSlugs, force));
      } catch (err) {
        console.error(`  ERROR ${campaign.id}: ${err.message || err}`);
        summary.push({ campaign: campaign.id, exported: [], failed: [{ slug: '*', error: String(err.message || err) }] });
      }
    }
  } finally {
    await browser.close();
  }

  console.log('\n=== Summary ===');
  for (const row of summary) {
    const status = row.failed?.length
      ? `${row.exported.length} saved, ${row.failed.length} failed`
      : row.exported.length
        ? `${row.exported.length} saved`
        : 'complete/skipped';
    console.log(`${row.campaign}: ${status}`);
    for (const f of row.failed || []) {
      console.log(`  - ${f.slug}: ${f.error}`);
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
