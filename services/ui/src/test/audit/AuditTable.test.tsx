import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AuditTable } from '../../components/audit/AuditTable'
import { auditFixture } from '../../fixtures/audit'

describe('AuditTable', () => {
  it('renders four column headers: Time, Agent, Action, Tokens', () => {
    render(
      <MemoryRouter>
        <AuditTable entries={[]} />
      </MemoryRouter>
    )
    expect(screen.getByText('Time')).toBeInTheDocument()
    expect(screen.getByText('Agent')).toBeInTheDocument()
    expect(screen.getByText('Action')).toBeInTheDocument()
    expect(screen.getByText('Tokens')).toBeInTheDocument()
  })

  it('renders one row per entry in the entries prop', () => {
    render(
      <MemoryRouter>
        <AuditTable entries={auditFixture} />
      </MemoryRouter>
    )
    // Each entry has a unique time value rendered in the first cell
    auditFixture.forEach(entry => {
      expect(screen.getByText(entry.time)).toBeInTheDocument()
    })
    // Verify the count matches the fixture length
    const rows = screen.getAllByTestId('main-row')
    expect(rows).toHaveLength(auditFixture.length)
  })
})
