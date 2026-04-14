/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{html,js,svelte}'],
  theme: {
    extend: {
      colors: {
        ink: '#04070d',
        live: '#5ee7ff',
        alert: '#ff4d8d',
        panel: '#0c131d',
        steel: '#8b9bb2',
        line: '#223144'
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'monospace'],
        display: ['"Space Grotesk"', 'sans-serif']
      },
      boxShadow: {
        panel: '0 18px 50px rgba(0, 0, 0, 0.38)',
        live: '0 0 0 1px rgba(94, 231, 255, 0.18)',
        alert: '0 0 0 1px rgba(255, 77, 141, 0.28)'
      }
    }
  },
  plugins: []
};
