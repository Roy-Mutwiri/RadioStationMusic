/**
 * TRADE FIX RADIO — design tokens.
 *
 * The brief asks for a professional broadcast console, not a SaaS dashboard: near-black,
 * restrained gold, dense but readable, subtle borders, controlled glow, no neon.
 *
 * Two decisions do most of the work here.
 *
 * **Gold is an accent, not a theme.** It marks exactly one thing per screen — the live
 * indicator, the active nav item, the value the eye should land on. The moment a second
 * element claims it, it stops meaning anything. Everything else is built from the ink scale.
 *
 * **Status colours are muted on purpose.** A saturated red next to a saturated gold is the
 * crypto-casino look the brief rules out, and a console an operator watches for hours has to
 * be quiet when nothing is wrong. These are desaturated enough to live on charcoal and still
 * clear enough to read at a glance — and every status is *also* carried by a label and an
 * icon shape, because §accessibility forbids encoding health in colour alone.
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // The ink scale. 950 is the page, 900 the panels, 800 the raised surfaces.
        ink: {
          950: '#07080a',
          900: '#0c0e12',
          850: '#101319',
          800: '#151922',
          750: '#1b202b',
          700: '#232935',
          600: '#2f3744',
          500: '#444d5d',
          400: '#6b7383',
          300: '#9aa1ad',
          200: '#c2c7d0',
          100: '#e6e8ec',
        },
        gold: {
          500: '#c9a227',
          400: '#d9b949',
          300: '#e7cd74',
          200: '#f0e0a6',
          dim: '#8a6f1c',
        },
        status: {
          healthy: '#4f9d69',
          degraded: '#c9962f',
          critical: '#c2504a',
          recovering: '#4a7fc2',
          offline: '#6b7383',
        },
        market: {
          up: '#4f9d69',
          down: '#c2504a',
          flat: '#9aa1ad',
        },
      },
      fontFamily: {
        // A UI stack that reads well dense, and a mono stack for every number that must
        // line up in a column — prices, durations, BPM, job ids.
        sans: ['Inter', 'Segoe UI', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Cascadia Mono', 'Consolas', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.04em' }],
        xs: ['0.75rem', { lineHeight: '1.1rem' }],
      },
      letterSpacing: { label: '0.08em' },
      borderRadius: { panel: '4px' },
      boxShadow: {
        // "Controlled glow": a tight ring, never a bloom.
        glow: '0 0 0 1px rgba(201,162,39,0.35), 0 0 12px -4px rgba(201,162,39,0.45)',
        panel: '0 1px 0 0 rgba(255,255,255,0.03) inset',
      },
      animation: {
        'pulse-live': 'pulse-live 2.4s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
      keyframes: {
        'pulse-live': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.45' },
        },
      },
    },
  },
  plugins: [],
}
