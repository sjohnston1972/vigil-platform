import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExpandableRow } from '../../components/audit/ExpandableRow'
import type { AuditEntry } from '../../types'

const entry: AuditEntry = {
  id: 'a1', time: '14:22:01', agent: 'network_agent',
  action: 'BGP audit — 10.0.0.1', tokens: 1240, isStepUp: false,
  sessionId: 'sess_8f2a', agentsInvolved: ['network_agent', 'rag_agent'], durationMs: 3100,
}

const stepUpEntry: AuditEntry = { ...entry, id: 'a2', agent: 'step-up', isStepUp: true, tokens: null, durationMs: null, agentsInvolved: [] }

describe('ExpandableRow', () => {
  it('renders time, agent, action, tokens', () => {
    render(<table><tbody><ExpandableRow entry={entry} /></tbody></table>)
    expect(screen.getByText('14:22:01')).toBeInTheDocument()
    expect(screen.getByText('network_agent')).toBeInTheDocument()
    expect(screen.getByText('BGP audit — 10.0.0.1')).toBeInTheDocument()
    expect(screen.getByText('1,240')).toBeInTheDocument()
  })

  it('shows — for null token count', () => {
    render(<table><tbody><ExpandableRow entry={stepUpEntry} /></tbody></table>)
    expect(screen.getByTestId('tokens-cell')).toHaveTextContent('—')
  })

  it('step-up row has amber background class', () => {
    render(<table><tbody><ExpandableRow entry={stepUpEntry} /></tbody></table>)
    expect(screen.getByTestId('main-row')).toHaveClass('bg-vigil-warn-row')
  })

  it('does not show detail row initially', () => {
    render(<table><tbody><ExpandableRow entry={entry} /></tbody></table>)
    expect(screen.queryByText('sess_8f2a')).not.toBeInTheDocument()
  })

  it('expands detail row on click', async () => {
    render(<table><tbody><ExpandableRow entry={entry} /></tbody></table>)
    await userEvent.click(screen.getByTestId('main-row'))
    expect(screen.getByText('sess_8f2a')).toBeInTheDocument()
    expect(screen.getByText(/network_agent, rag_agent/)).toBeInTheDocument()
  })

  it('collapses on second click', async () => {
    render(<table><tbody><ExpandableRow entry={entry} /></tbody></table>)
    await userEvent.click(screen.getByTestId('main-row'))
    await userEvent.click(screen.getByTestId('main-row'))
    expect(screen.queryByText('sess_8f2a')).not.toBeInTheDocument()
  })
})
