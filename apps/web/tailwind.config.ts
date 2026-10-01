import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    screens: {
      xs: '400px',
      sm: '640px',
      md: '768px',
      lg: '1024px',
      xl: '1280px',
      '2xl': '1536px',
    },
    extend: {
      colors: {
        brand: {
          50: 'rgba(240,180,41,0.1)',
          100: 'rgba(240,180,41,0.15)',
          200: 'rgba(240,180,41,0.25)',
          300: '#F7CC5F',
          400: '#F0B429',
          500: '#F0B429',
          600: '#F0B429',
          700: '#D4A017',
          800: '#8B6914',
          900: '#5C4510',
          950: '#2E220A',
        },
        severity: {
          hard: '#b91c1c',
          critical: '#dc2626',
          warning: '#d97706',
          info: '#2563eb',
        },
        confidence: {
          high: '#16a34a',
          medium: '#d97706',
          low: '#dc2626',
        },
      },
      fontFamily: {
        sans: [
          'Schibsted Grotesk',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'sans-serif',
        ],
        display: ['Instrument Serif', 'Georgia', 'serif'],
        mono: ['IBM Plex Mono', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 3px 0 rgb(0 0 0 / 0.2), 0 1px 2px -1px rgb(0 0 0 / 0.2)',
        'card-hover': '0 4px 6px -1px rgb(0 0 0 / 0.25), 0 2px 4px -2px rgb(0 0 0 / 0.25)',
        elevated: '0 10px 15px -3px rgb(0 0 0 / 0.3), 0 4px 6px -4px rgb(0 0 0 / 0.3)',
        nav: '0 1px 3px 0 rgb(0 0 0 / 0.2)',
        gold: '0 0 20px rgba(240,180,41,0.15)',
      },
      animation: {
        'fade-in': 'fadeIn 0.3s ease-out',
        'slide-up': 'slideUp 0.3s ease-out',
        'slide-down': 'slideDown 0.2s ease-out',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideDown: {
          '0%': { opacity: '0', transform: 'translateY(-4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
};

export default config;
