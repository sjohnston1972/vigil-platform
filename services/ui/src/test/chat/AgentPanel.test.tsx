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

  it('renders two distinct rows for two invocations of the same agent, one running and one complete, with no duplicate React keys', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const sameAgentGroups: MessageAgentGroup[] = [
      {
        messageIndex: 1,
        rows: [
          { id: 'row-a', agent: 'network_agent', status: 'complete', durationMs: 300 },
          { id: 'row-b', agent: 'network_agent', status: 'running' },
        ],
      },
    ]
    render(<AgentPanel groups={sameAgentGroups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))

    const dots = screen.getAllByTestId('dot-network_agent')
    expect(dots).toHaveLength(2)
    expect(dots.some(d => d.className.includes('bg-green-500'))).toBe(true)
    expect(dots.some(d => d.className.includes('bg-amber-500'))).toBe(true)

    // React logs a console.error when sibling elements share a key.
    const keyWarning = consoleError.mock.calls.some(call =>
      call.some(arg => typeof arg === 'string' && arg.includes('same key'))
    )
    expect(keyWarning).toBe(false)
    consoleError.mockRestore()
  })

  it('renders "0.0s" for a row with durationMs === 0, instead of hiding it', async () => {
    const zeroDurationGroups: MessageAgentGroup[] = [
      { messageIndex: 1, rows: [{ id: 'row-z', agent: 'enrichment_agent', status: 'complete', durationMs: 0 }] },
    ]
    render(<AgentPanel groups={zeroDurationGroups} totalTokens={0} />)
    await userEvent.click(screen.getByRole('button', { name: /expand/i }))
    expect(screen.getByText('0.0s')).toBeInTheDocument()
  })
})
