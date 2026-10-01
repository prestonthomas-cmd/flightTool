import type { NextConfig } from 'next';

const config: NextConfig = {
  // The dashboard is per-user and reads cookies, so none of it is static.
  // Saying so here keeps a stray build-time prerender from failing the deploy.
  experimental: {},
};

export default config;
