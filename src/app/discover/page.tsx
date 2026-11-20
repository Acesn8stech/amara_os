import type { Metadata } from 'next';
import DiscoverLoader from '@/components/discover/DiscoverLoader';

export const metadata: Metadata = {
  title: 'AMARA Discover',
  description: 'Search and explore land parcels on an interactive map.',
};

export default function DiscoverPage() {
  return <DiscoverLoader />;
}
