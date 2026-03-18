import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionItem } from '../../components/chat/SessionItem'
import type { Session } from '../../types'

const session: Session = {
  id: 's1',
  title: 'BGP audit — router-core-01',
  agents: ['net', 'rag'],
  updatedAt: new Date().toISOString(),
}

describe('SessionItem', () => {
  it('renders session title', () => {
    render(<SessionItem session={session} isActive={false} onSelect={() => {}} onRename={() => {}} />)
    expect(screen.getByText('BGP audit — router-core-01')).toBeInTheDocument()
  })

  it('renders agent badges', () => {
    render(<SessionItem session={session} isActive={false} onSelect={() => {}} onRename={() => {}} />)
    expect(screen.getByText('net')).toBeInTheDocument()
    expect(screen.getByText('rag')).toBeInTheDocument()
  })

  it('shows active style when isActive', () => {
    render(<SessionItem session={session} isActive={true} onSelect={() => {}} onRename={() => {}} />)
    expect(screen.getByTestId('session-item')).toHaveClass('border-vigil-accent')
  })

  it('enters rename mode on double-click', async () => {
    render(<SessionItem session={session} isActive={false} onSelect={() => {}} onRename={() => {}} />)
    await userEvent.dblClick(screen.getByText('BGP audit — router-core-01'))
    expect(screen.getByRole('textbox')).toBeInTheDocument()
  })

  it('calls onRename with new title on Enter', async () => {
    const onRename = vi.fn()
    render(<SessionItem session={session} isActive={false} onSelect={() => {}} onRename={onRename} />)
    await userEvent.dblClick(screen.getByText('BGP audit — router-core-01'))
    const input = screen.getByRole('textbox')
    await userEvent.clear(input)
    await userEvent.type(input, 'New title{Enter}')
    expect(onRename).toHaveBeenCalledWith('s1', 'New title')
  })

  it('calls onRename on blur', async () => {
    const onRename = vi.fn()
    render(<SessionItem session={session} isActive={false} onSelect={() => {}} onRename={onRename} />)
    await userEvent.dblClick(screen.getByText('BGP audit — router-core-01'))
    const input = screen.getByRole('textbox')
    await userEvent.clear(input)
    await userEvent.type(input, 'Blur title')
    await userEvent.tab()
    expect(onRename).toHaveBeenCalledWith('s1', 'Blur title')
  })
})
