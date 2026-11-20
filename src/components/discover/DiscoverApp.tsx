'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Image from 'next/image';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { DEMO_CENTER } from '@/lib/discover/demo-data';
import {
  DEFAULT_FILTERS,
  type DiscoverFilters,
  type GeocodeResponse,
  type Parcel,
  type ParcelsResponse,
} from '@/lib/discover/types';

type BaseLayerId = 'satellite' | 'streets';
type Tab = 'results' | 'saved';

const BASE_LAYERS: Record<BaseLayerId, { url: string; attribution: string; maxZoom: number }> = {
  satellite: {
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics',
    maxZoom: 19,
  },
  streets: {
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
  },
};

const SAVED_KEY = 'amara-discover-saved';
const MIN_PARCEL_ZOOM = 14;

const fmtAcres = (n: number) => (n >= 100 ? n.toFixed(0) : n.toFixed(2));

function readSaved(): Parcel[] {
  try {
    const raw = window.localStorage.getItem(SAVED_KEY);
    return raw ? (JSON.parse(raw) as Parcel[]) : [];
  } catch {
    return [];
  }
}

function writeSaved(parcels: Parcel[]) {
  try {
    window.localStorage.setItem(SAVED_KEY, JSON.stringify(parcels));
  } catch {
    // Storage unavailable (private mode etc.) — saved list is session-only.
  }
}

function matchesFilters(p: Parcel, f: DiscoverFilters): boolean {
  const min = f.minAcres === '' ? null : Number(f.minAcres);
  const max = f.maxAcres === '' ? null : Number(f.maxAcres);
  if (min !== null && Number.isFinite(min) && p.acres < min) return false;
  if (max !== null && Number.isFinite(max) && p.acres > max) return false;
  if (f.zoning !== 'all' && p.zoning !== f.zoning) return false;
  const owner = f.owner.trim().toLowerCase();
  if (owner && !p.owner.toLowerCase().includes(owner)) return false;
  return true;
}

export default function DiscoverApp() {
  const mapEl = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const baseRef = useRef<L.TileLayer | null>(null);
  const parcelLayerRef = useRef<L.LayerGroup | null>(null);
  const pinRef = useRef<L.CircleMarker | null>(null);
  const fetchAbort = useRef<AbortController | null>(null);

  const [parcels, setParcels] = useState<Parcel[]>([]);
  const [source, setSource] = useState<'live' | 'demo' | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [zoomedOut, setZoomedOut] = useState(false);

  const [baseLayer, setBaseLayer] = useState<BaseLayerId>('satellite');
  const [filters, setFilters] = useState<DiscoverFilters>(DEFAULT_FILTERS);
  const [selected, setSelected] = useState<Parcel | null>(null);
  const [saved, setSaved] = useState<Parcel[]>([]);
  const [savedReady, setSavedReady] = useState(false);
  const [tab, setTab] = useState<Tab>('results');
  const [panelOpen, setPanelOpen] = useState(true);

  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<GeocodeResponse | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);

  const selectedRef = useRef<Parcel | null>(null);
  selectedRef.current = selected;

  const visible = useMemo(() => parcels.filter((p) => matchesFilters(p, filters)), [parcels, filters]);
  const zoningOptions = useMemo(
    () => ['all', ...Array.from(new Set(parcels.map((p) => p.zoning))).sort()],
    [parcels],
  );
  const savedIds = useMemo(() => new Set(saved.map((p) => p.id)), [saved]);

  // ---- load saved list from localStorage (client only) --------------------
  useEffect(() => {
    const id = window.setTimeout(() => {
      setSaved(readSaved());
      setSavedReady(true);
    }, 0);
    return () => window.clearTimeout(id);
  }, []);

  useEffect(() => {
    if (savedReady) writeSaved(saved);
  }, [saved, savedReady]);

  // ---- fetch parcels for the current viewport -----------------------------
  const loadParcels = useCallback(async () => {
    const map = mapRef.current;
    if (!map) return;
    if (map.getZoom() < MIN_PARCEL_ZOOM) {
      fetchAbort.current?.abort();
      setZoomedOut(true);
      setLoading(false);
      return;
    }
    setZoomedOut(false);
    const b = map.getBounds();
    fetchAbort.current?.abort();
    const ctrl = new AbortController();
    fetchAbort.current = ctrl;
    setLoading(true);
    try {
      const res = await fetch(
        `/api/discover/parcels?bbox=${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`,
        { signal: ctrl.signal },
      );
      if (!res.ok) throw new Error(`Parcel request failed (${res.status})`);
      const data = (await res.json()) as ParcelsResponse;
      setParcels(data.parcels);
      setSource(data.source);
      setNote(data.note ?? null);
    } catch (e) {
      if ((e as Error).name === 'AbortError') return;
      setNote(e instanceof Error ? e.message : 'Could not load parcels');
    } finally {
      if (fetchAbort.current === ctrl) setLoading(false);
    }
  }, []);

  // ---- map init (once) -----------------------------------------------------
  useEffect(() => {
    if (!mapEl.current || mapRef.current) return;
    const map = L.map(mapEl.current, { zoomControl: false, preferCanvas: true }).setView(
      DEMO_CENTER,
      15,
    );
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    L.control.scale({ position: 'bottomleft', imperial: true }).addTo(map);
    mapRef.current = map;
    parcelLayerRef.current = L.layerGroup().addTo(map);

    let timer: number | undefined;
    map.on('moveend', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void loadParcels(), 250);
    });
    map.whenReady(() => map.fire('moveend'));

    return () => {
      window.clearTimeout(timer);
      fetchAbort.current?.abort();
      map.remove();
      mapRef.current = null;
      baseRef.current = null;
      parcelLayerRef.current = null;
      pinRef.current = null;
    };
  }, [loadParcels]);

  // ---- base layer ----------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    baseRef.current?.remove();
    const cfg = BASE_LAYERS[baseLayer];
    baseRef.current = L.tileLayer(cfg.url, {
      attribution: cfg.attribution,
      maxZoom: cfg.maxZoom,
    })
      .addTo(map)
      .bringToBack();
  }, [baseLayer]);

  // ---- draw parcels --------------------------------------------------------
  useEffect(() => {
    const group = parcelLayerRef.current;
    if (!group) return;
    group.clearLayers();
    for (const p of visible) {
      const isSel = selected?.id === p.id;
      const isSaved = savedIds.has(p.id);
      const color = isSel ? '#ffffff' : isSaved ? '#ffd24a' : '#9db8ff';
      const poly = L.polygon(
        p.rings.map((ring) => ring.map(([lng, lat]) => [lat, lng] as L.LatLngTuple)),
        {
          color,
          weight: isSel ? 3 : 1.5,
          fillColor: isSel ? '#9db8ff' : color,
          fillOpacity: isSel ? 0.35 : 0.12,
        },
      );
      poly.on('click', () => {
        setSelected(p);
        setPanelOpen(true);
      });
      poly.bindTooltip(`${p.apn} · ${fmtAcres(p.acres)} ac`, { sticky: true });
      group.addLayer(poly);
    }
  }, [visible, selected, savedIds]);

  // ---- actions -------------------------------------------------------------
  const flyTo = useCallback((lat: number, lng: number, bbox?: [number, number, number, number]) => {
    const map = mapRef.current;
    if (!map) return;
    pinRef.current?.remove();
    pinRef.current = L.circleMarker([lat, lng], {
      radius: 8,
      color: '#ffffff',
      weight: 2,
      fillColor: '#ff3b6b',
      fillOpacity: 0.9,
    }).addTo(map);
    if (bbox) {
      map.fitBounds(
        [
          [bbox[0], bbox[1]],
          [bbox[2], bbox[3]],
        ],
        { maxZoom: 17 },
      );
    } else {
      map.setView([lat, lng], 17);
    }
  }, []);

  const selectParcel = useCallback(
    (p: Parcel) => {
      setSelected(p);
      setPanelOpen(true);
      flyTo(p.center[0], p.center[1]);
    },
    [flyTo],
  );

  const toggleSaved = useCallback((p: Parcel) => {
    setSaved((prev) => (prev.some((s) => s.id === p.id) ? prev.filter((s) => s.id !== p.id) : [p, ...prev]));
  }, []);

  const runSearch = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (q.length < 2) return;
    setSearching(true);
    setSearchError(null);
    try {
      const res = await fetch(`/api/discover/geocode?q=${encodeURIComponent(q)}`);
      if (!res.ok) throw new Error(`Search failed (${res.status})`);
      setSearchResults((await res.json()) as GeocodeResponse);
    } catch (err) {
      setSearchResults(null);
      setSearchError(err instanceof Error ? err.message : 'Search failed');
    } finally {
      setSearching(false);
    }
  }, [query]);

  const clearSearch = () => {
    setQuery('');
    setSearchResults(null);
    setSearchError(null);
  };

  const list = tab === 'saved' ? saved : visible;
  const filtersActive =
    filters.minAcres !== '' || filters.maxAcres !== '' || filters.zoning !== 'all' || filters.owner !== '';

  const input =
    'w-full rounded-xl border border-white/15 bg-white/5 px-3 py-2 text-sm text-white placeholder-white/40 outline-none focus:border-[#9db8ff]';

  return (
    <div className="fixed inset-0 flex cursor-default bg-[#05060b] text-white [&_button]:cursor-pointer">
      {/* ------------------------------ sidebar ------------------------------ */}
      <aside
        className={`absolute inset-y-3 left-3 z-[1100] flex w-[calc(100%-1.5rem)] flex-col overflow-hidden rounded-3xl border border-white/10 bg-[radial-gradient(120%_60%_at_50%_0%,rgba(157,184,255,0.10),transparent_60%)] bg-[#05060b]/90 shadow-[0_20px_80px_rgba(0,0,0,0.7)] backdrop-blur-xl sm:w-96 ${
          panelOpen ? '' : '-translate-x-[120%]'
        } transition-transform duration-300`}
      >
        <header className="border-b border-white/10 p-4">
          <div className="flex items-center gap-3">
            <Image
              src="/amara-face.jpg"
              alt="AMARA"
              width={52}
              height={52}
              priority
              className="h-13 w-13 rounded-full object-cover ring-2 ring-[#9db8ff]/60 shadow-[0_0_24px_rgba(157,184,255,0.45)]"
            />
            <div>
              <h1 className="text-lg font-light uppercase tracking-[0.3em] text-white">Amara</h1>
              <p className="text-xs tracking-wide text-[#9db8ff]">Land discovery</p>
            </div>
          </div>
          <form onSubmit={runSearch} className="mt-3 flex gap-2">
            <input
              className={input}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Address, city, APN or owner"
              aria-label="Search"
            />
            <button
              type="submit"
              disabled={searching || query.trim().length < 2}
              className="rounded-xl bg-[#9db8ff] px-3 text-sm font-medium text-black disabled:opacity-40"
            >
              {searching ? '…' : 'Go'}
            </button>
          </form>

          {(searchResults || searchError) && (
            <div className="mt-2 max-h-56 overflow-auto rounded-xl border border-white/10 bg-black/40 text-sm">
              <button
                onClick={clearSearch}
                className="w-full border-b border-white/10 px-3 py-1 text-right text-xs text-white/50 hover:text-white"
              >
                Clear results
              </button>
              {searchError && <p className="px-3 py-2 text-red-400">{searchError}</p>}
              {searchResults?.parcelMatches.map((p) => (
                <button
                  key={p.id}
                  onClick={() => selectParcel(p)}
                  className="block w-full px-3 py-2 text-left hover:bg-white/10"
                >
                  <span className="text-[#c4d3ff]">Parcel {p.apn}</span>
                  <span className="block text-xs text-white/60">
                    {p.owner} · {p.siteAddress}
                  </span>
                </button>
              ))}
              {searchResults?.results.map((r) => (
                <button
                  key={`${r.lat},${r.lng}`}
                  onClick={() => flyTo(r.lat, r.lng, r.bbox)}
                  className="block w-full px-3 py-2 text-left hover:bg-white/10"
                >
                  {r.label}
                </button>
              ))}
              {searchResults &&
                searchResults.parcelMatches.length === 0 &&
                searchResults.results.length === 0 && (
                  <p className="px-3 py-2 text-white/60">No matches.</p>
                )}
            </div>
          )}
        </header>

        <section className="grid grid-cols-2 gap-2 border-b border-white/10 p-4 text-sm">
          <label className="text-xs text-white/50">
            Min acres
            <input
              className={`${input} mt-1`}
              inputMode="decimal"
              value={filters.minAcres}
              onChange={(e) => setFilters({ ...filters, minAcres: e.target.value })}
            />
          </label>
          <label className="text-xs text-white/50">
            Max acres
            <input
              className={`${input} mt-1`}
              inputMode="decimal"
              value={filters.maxAcres}
              onChange={(e) => setFilters({ ...filters, maxAcres: e.target.value })}
            />
          </label>
          <label className="text-xs text-white/50">
            Zoning
            <select
              className={`${input} mt-1`}
              value={filters.zoning}
              onChange={(e) => setFilters({ ...filters, zoning: e.target.value })}
            >
              {zoningOptions.map((z) => (
                <option key={z} value={z} className="bg-[#05060b]">
                  {z === 'all' ? 'All zoning' : z}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-white/50">
            Owner contains
            <input
              className={`${input} mt-1`}
              value={filters.owner}
              onChange={(e) => setFilters({ ...filters, owner: e.target.value })}
            />
          </label>
          {filtersActive && (
            <button
              onClick={() => setFilters(DEFAULT_FILTERS)}
              className="col-span-2 text-left text-xs text-[#9db8ff] hover:underline"
            >
              Reset filters
            </button>
          )}
        </section>

        <nav className="flex border-b border-white/10 text-sm">
          {(['results', 'saved'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 px-4 py-2 capitalize ${
                tab === t ? 'border-b-2 border-[#9db8ff] text-white' : 'text-white/50'
              }`}
            >
              {t === 'results' ? `In view (${visible.length})` : `Saved (${saved.length})`}
            </button>
          ))}
        </nav>

        <div className="min-h-0 flex-1 overflow-auto">
          {tab === 'results' && zoomedOut && (
            <p className="p-4 text-sm text-white/60">Zoom in to see parcels.</p>
          )}
          {list.length === 0 && !(tab === 'results' && zoomedOut) && (
            <p className="p-4 text-sm text-white/60">
              {tab === 'saved'
                ? 'Nothing saved yet. Select a parcel and tap Save.'
                : loading
                  ? 'Loading parcels…'
                  : 'No parcels match here.'}
            </p>
          )}
          <ul>
            {list.map((p) => (
              <li key={p.id}>
                <button
                  onClick={() => selectParcel(p)}
                  className={`block w-full border-b border-white/5 px-4 py-3 text-left hover:bg-white/5 ${
                    selected?.id === p.id ? 'bg-[#9db8ff]/10' : ''
                  }`}
                >
                  <span className="flex items-baseline justify-between gap-2">
                    <span className="text-sm font-medium">{p.siteAddress}</span>
                    <span className="shrink-0 text-xs text-[#c4d3ff]">{fmtAcres(p.acres)} ac</span>
                  </span>
                  <span className="block text-xs text-white/50">
                    {p.owner} · {p.zoning} · APN {p.apn}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <footer className="border-t border-white/10 px-4 py-2 text-xs text-white/50">
          {source === 'demo' && (
            <p className="mb-1 rounded bg-amber-400/15 px-2 py-1 text-amber-300">
              Demo data — not real parcels.
            </p>
          )}
          {note && <p>{note}</p>}
        </footer>
      </aside>

      {/* -------------------------------- map -------------------------------- */}
      <main className="relative h-full w-full">
        <div ref={mapEl} className="h-full w-full !bg-[#05060b]" />

        <button
          onClick={() => setPanelOpen((o) => !o)}
          className={`absolute top-3 z-[1200] rounded-xl border border-white/20 bg-[#05060b]/90 px-3 py-2 text-sm ${
            panelOpen ? 'right-6 sm:right-auto sm:left-[25.5rem]' : 'left-3'
          }`}
          aria-label={panelOpen ? 'Hide panel' : 'Show panel'}
        >
          {panelOpen ? '‹' : '☰'}
        </button>

        <div className="absolute right-3 top-3 z-[1050] flex overflow-hidden rounded-xl border border-white/20 bg-[#05060b]/90 text-sm">
          {(['satellite', 'streets'] as const).map((id) => (
            <button
              key={id}
              onClick={() => setBaseLayer(id)}
              className={`px-3 py-2 capitalize ${baseLayer === id ? 'bg-[#9db8ff] text-black' : ''}`}
            >
              {id}
            </button>
          ))}
        </div>

        {loading && (
          <div className="absolute left-1/2 top-3 z-[1050] -translate-x-1/2 rounded-full bg-[#05060b]/90 px-3 py-1 text-xs text-[#c4d3ff]">
            Loading parcels…
          </div>
        )}

        {/* --------------------------- parcel detail --------------------------- */}
        {selected && (
          <div className="absolute inset-x-3 bottom-3 z-[1050] rounded-3xl border border-white/10 bg-[#05060b]/85 p-5 shadow-[0_20px_80px_rgba(0,0,0,0.7)] backdrop-blur-xl sm:inset-x-auto sm:right-3 sm:top-16 sm:bottom-auto sm:w-80">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="text-base font-semibold">{selected.siteAddress}</h2>
                <p className="text-xs text-white/50">
                  {[selected.city, selected.county && `${selected.county} County`].filter(Boolean).join(' · ')}
                </p>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="text-white/50 hover:text-white"
                aria-label="Close parcel details"
              >
                ✕
              </button>
            </div>
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-white/50">Owner</dt>
              <dd>{selected.owner}</dd>
              <dt className="text-white/50">APN</dt>
              <dd className="font-mono">{selected.apn}</dd>
              <dt className="text-white/50">Acres</dt>
              <dd>{fmtAcres(selected.acres)}</dd>
              <dt className="text-white/50">Zoning</dt>
              <dd>{selected.zoning}</dd>
            </dl>
            <div className="mt-4 flex flex-wrap gap-2 text-sm">
              <button
                onClick={() => toggleSaved(selected)}
                className={`rounded-xl px-3 py-1.5 font-medium ${
                  savedIds.has(selected.id) ? 'bg-amber-400 text-black' : 'bg-[#9db8ff] text-black'
                }`}
              >
                {savedIds.has(selected.id) ? 'Saved ✓' : 'Save'}
              </button>
              <button
                onClick={() => void navigator.clipboard?.writeText(selected.apn)}
                className="rounded-xl border border-white/20 px-3 py-1.5"
              >
                Copy APN
              </button>
              <a
                href={`https://www.google.com/maps/search/?api=1&query=${selected.center[0]},${selected.center[1]}`}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-xl border border-white/20 px-3 py-1.5"
              >
                Google Maps ↗
              </a>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
