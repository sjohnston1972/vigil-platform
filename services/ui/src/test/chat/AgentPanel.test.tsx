import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AgentPanel } from '../../components/chat/AgentPanel'
import type { MessageAgentGroup } from '../../types'

const groups: MessageAgentGroup[] = [
  {
    messageIndex: 1,
    rows: [
      { id: 'row-1', agent: 'network_agent', status: 'complete', durationMs: 1240 },
      { id: 'row-2', agent: 'rag_agent',     status: 'running' },
    ],
  },
]

describe('AgentPanel', () => {
  it('shows AGENTS label when collapsed', () => {
    render(<AgentPanel groups={groups} totalTokens={0} />)
    expect(screen.getByText('AGENTS')).toBeInTheDocument()
  })

  it('does not show agent names when collapsed', () => {
    render(<AgentPanel groups={groups} totalTokens={0} />)
    expect(screen.queryByText('network_agent')).not.toBeInTheDocument()
  })

  it('expands when expand button is clicked', async () => {
    render(<AgentPanel groups={groups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))
    expect(screen.getByText('network_agent')).toBeInTheDocument()
  })

  it('shows green dot for complete agents', async () => {
    render(<AgentPanel groups={groups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))
    const dot = screen.getByTestId('dot-network_agent')
    expect(dot).toHaveClass('bg-green-500')
  })

  it('shows amber dot for running agents', async () => {
    render(<AgentPanel groups={groups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))
    expect(screen.getByTestId('dot-rag_agent')).toHaveClass('bg-amber-500')
  })

  it('collapses when collapse button is clicked', async () => {
    render(<AgentPanel groups={groups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))
    await userEvent.click(screen.getByRole('button', { name: /collapse/i }))
    expect(screen.queryByText('network_agent')).not.toBeInTheDocument()
  })

  it('collapses a message group when its header is clicked', async () => {
    render(<AgentPanel groups={groups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))
    // Groups start open — click the group header to collapse it
    await userEvent.click(screen.getByRole('button', { name: /toggle message 1/i }))
    expect(screen.queryByText('network_agent')).not.toBeInTheDocument()
  })

  it('shows red dot for error agents', async () => {
    const errorGroups: MessageAgentGroup[] = [
      { messageIndex: 1, rows: [{ id: 'row-3', agent: 'itsm_agent', status: 'error' }] },
    ]
    render(<AgentPanel groups={errorGroups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))
    expect(screen.getByTestId('dot-itsm_agent')).toHaveClass('bg-red-500')
  })
})
