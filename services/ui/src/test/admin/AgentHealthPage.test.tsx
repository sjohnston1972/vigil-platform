import { render, screen } from '@testing-library/react'
import { AgentHealthPage } from '../../components/admin/AgentHealthPage'

describe('AgentHealthPage', () => {
  it('renders column headers: Agent, Status, Last Heartbeat, p95 Response', () => {
    render(<AgentHealthPage />)
    const headers = screen.getAllByRole('columnheader').map(h => h.textContent)
    expect(headers).toContain('Agent')
    expect(headers).toContain('Status')
    expect(headers).toContain('Last Heartbeat')
    expect(headers).toContain('p95 Response')
  })

  it('stale agent row has bg-vigil-warn-row class', () => {
    render(<AgentHealthPage />)
    // enrichment_agent has status: 'stale' in the fixture
    const agentCell = screen.getByText('enrichment_agent')
    const row = agentCell.closest('tr')
    expect(row).toHaveClass('bg-vigil-warn-row')
  })
})
