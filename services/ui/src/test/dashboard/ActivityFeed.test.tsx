import { render, screen } from '@testing-library/react'
import { ActivityFeed } from '../../components/dashboard/ActivityFeed'
import type { ActivityEntry } from '../../types'

const entries: ActivityEntry[] = [
  { id: 'a1', icon: '✅', description: 'BGP audit — 10.0.0.1', timestamp: '2m ago' },
  { id: 'a2', icon: '⏳', description: 'Step-up pending', timestamp: '5m ago', isStepUp: true },
]

describe('ActivityFeed', () => {
  it('renders all entries', () => {
    render(<ActivityFeed entries={entries} />)
    expect(screen.getByText('BGP audit — 10.0.0.1')).toBeInTheDocument()
    expect(screen.getByText('Step-up pending')).toBeInTheDocument()
  })

  it('renders step-up entries with amber text class', () => {
    render(<ActivityFeed entries={entries} />)
    const stepUpRow = screen.getByText('Step-up pending').closest('[data-testid="activity-row"]')
    expect(stepUpRow).toHaveClass('text-amber-400')
  })
})
