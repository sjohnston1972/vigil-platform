import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FilterBar } from '../../components/audit/FilterBar'

describe('FilterBar', () => {
  it('renders search input', () => {
    render(<FilterBar onSearchChange={() => {}} onAgentChange={() => {}} onDateChange={() => {}} agents={[]} />)
    expect(screen.getByPlaceholderText(/search/i)).toBeInTheDocument()
  })

  it('calls onSearchChange when typing', async () => {
    const onSearchChange = vi.fn()
    render(<FilterBar onSearchChange={onSearchChange} onAgentChange={() => {}} onDateChange={() => {}} agents={[]} />)
    await userEvent.type(screen.getByPlaceholderText(/search/i), 'BGP')
    expect(onSearchChange).toHaveBeenCalled()
  })

  it('calls onAgentChange with the selected agent value', async () => {
    const onAgentChange = vi.fn()
    render(
      <FilterBar
        onSearchChange={() => {}}
        onAgentChange={onAgentChange}
        onDateChange={() => {}}
        agents={['network_agent', 'itsm_agent']}
      />
    )
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /filter by agent/i }), 'network_agent')
    expect(onAgentChange).toHaveBeenCalledWith('network_agent')
  })

  it('calls onDateChange with the selected date range value', async () => {
    const onDateChange = vi.fn()
    render(<FilterBar onSearchChange={() => {}} onAgentChange={() => {}} onDateChange={onDateChange} agents={[]} />)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /filter by date/i }), '7d')
    expect(onDateChange).toHaveBeenCalledWith('7d')
  })
})
