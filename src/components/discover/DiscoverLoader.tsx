'use client';

import dynamic from 'next/dynamic';

// Leaflet touches `window` on import, so the map must be client-only.
const DiscoverApp = dynamic(() => import('./DiscoverApp'), {
  ssr: false,
  loading: () => (
    <div className="fixed inset-0 flex items-center justify-center bg-[#0a0a0f] text-sm text-white/60">
      Loading map…
    </div>
  ),
});

export default function DiscoverLoader() {
  return <DiscoverApp />;
}
