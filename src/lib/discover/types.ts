export interface Parcel {
  id: string;
  apn: string;
  owner: string;
  siteAddress: string;
  city: string;
  county: string;
  zoning: string;
  acres: number;
  /** Closed ring(s) of [lng, lat] pairs — GeoJSON Polygon coordinates. */
  rings: number[][][];
  /** Centroid as [lat, lng]. */
  center: [number, number];
}

export interface ParcelsResponse {
  source: 'live' | 'demo';
  parcels: Parcel[];
  note?: string;
}

export interface GeocodeResult {
  label: string;
  lat: number;
  lng: number;
  /** [south, west, north, east] when the geocoder supplies one. */
  bbox?: [number, number, number, number];
}

export interface GeocodeResponse {
  results: GeocodeResult[];
  /** Demo parcels matching the query by APN / owner / address. */
  parcelMatches: Parcel[];
}

export interface DiscoverFilters {
  minAcres: string;
  maxAcres: string;
  zoning: string;
  owner: string;
}

export const DEFAULT_FILTERS: DiscoverFilters = {
  minAcres: '',
  maxAcres: '',
  zoning: 'all',
  owner: '',
};
