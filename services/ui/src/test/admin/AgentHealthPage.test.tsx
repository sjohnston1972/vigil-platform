import { render, screen } from '@testing-library/react'
import { AgentHealthPage } from '../../components/admin/AgentHealthPage'

describe('AgentHealthPage', () => {
  it('renders column headers: Agent, Status, Last Heartbeat, p95 Response', () => {
    render(<AgentHealthPage />)
    expect(screen.getByText('Agent')).toBeInTheDocument()
    expect(screen.getByText('Status')).toBeInTheDocument()
    expect(screen.getByText('Last Heartbeat')).toBeInTheDocument()
    expect(screen.getByText('p95 Response')).toBeInTheDocument()
  })

  it('stale agent row has bg-vigil-warn-row class', () => {
    render(<AgentHealthPage />)
    // enrichment_agent has status: 'stale' in the fixture
    const agentCell = screen.getByText('enrichment_agent')
    const row = agentCell.closest('tr')
    expect(row).toHaveClass('bg-vigil-warn-row')
  })
})
