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
})
