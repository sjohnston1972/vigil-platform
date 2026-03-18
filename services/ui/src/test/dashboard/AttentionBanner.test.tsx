import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AttentionBanner } from '../../components/dashboard/AttentionBanner'

describe('AttentionBanner', () => {
  it('renders nothing when no alerts', () => {
    const { container } = render(
      <AttentionBanner pendingApprovals={0} failures={0} onReview={() => {}} />
    )
    expect(container.firstChild).toBeNull()
  })

  it('renders when there are pending approvals', () => {
    render(<AttentionBanner pendingApprovals={1} failures={0} onReview={() => {}} />)
    expect(screen.getByText(/approval/i)).toBeInTheDocument()
  })

  it('renders when there are failures', () => {
    render(<AttentionBanner pendingApprovals={0} failures={2} onReview={() => {}} />)
    expect(screen.getByText(/failure/i)).toBeInTheDocument()
  })

  it('calls onReview when Review button clicked', async () => {
    const onReview = vi.fn()
    render(<AttentionBanner pendingApprovals={1} failures={0} onReview={onReview} />)
    await userEvent.click(screen.getByRole('button', { name: /review/i }))
    expect(onReview).toHaveBeenCalledOnce()
  })
})
