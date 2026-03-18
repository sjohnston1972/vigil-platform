import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        vigil: {
          bg:        '#0a0f1e',
          sidebar:   '#060c18',
          card:      '#0f1729',
          border:    '#1a2035',
          accent:    '#6366f1',
          'accent-text': '#818cf8',
          'body':    '#94a3b8',
          'bright':  '#e2e8f0',
          'muted':   '#475569',
          'msg-bg':  '#1e1b4b',
          'msg-text':'#c7d2fe',
          'stepup-bg':    '#1a1200',
          'stepup-border':'#f59e0b66',
          'warn-row': '#2d1f00',
        },
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
