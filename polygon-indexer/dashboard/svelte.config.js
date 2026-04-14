import adapter from '@sveltejs/adapter-static';

const config = {
  kit: {
    adapter: adapter({
      fallback: 'index.html'
    }),
    alias: {
      $components: 'src/lib/components',
      $lib: 'src/lib'
    }
  }
};

export default config;
