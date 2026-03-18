import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StepUpBanner } from '../../components/chat/StepUpBanner'

const props = {
  tool: 'apply_change',
  device: 'router-core-01',
  expiresAt: new Date(Date.now() + 900_000).toISOString(),  // 15 min from now
  onApprove: vi.fn(),
  onReject: vi.fn(),
}

describe('StepUpBanner', () => {
  it('renders tool and device name', () => {
    render(<StepUpBanner {...props} />)
    expect(screen.getByText(/apply_change/)).toBeInTheDocument()
    expect(screen.getByText(/router-core-01/)).toBeInTheDocument()
  })

  it('calls onApprove when Approve clicked', async () => {
    render(<StepUpBanner {...props} />)
    await userEvent.click(screen.getByRole('button', { name: /approve/i }))
    expect(props.onApprove).toHaveBeenCalledOnce()
  })

  it('calls onReject when Reject clicked', async () => {
    render(<StepUpBanner {...props} />)
    await userEvent.click(screen.getByRole('button', { name: /reject/i }))
    expect(props.onReject).toHaveBeenCalledOnce()
  })

  it('shows expiry countdown', () => {
    render(<StepUpBanner {...props} />)
    expect(screen.getByText(/expires/i)).toBeInTheDocument()
  })

  it('calls onExpire when countdown reaches zero', async () => {
    const onExpire = vi.fn()
    const alreadyExpired = new Date(Date.now() - 1000).toISOString()
    render(<StepUpBanner tool="apply_change" expiresAt={alreadyExpired} onApprove={() => {}} onReject={() => {}} onExpire={onExpire} />)
    await act(async () => { await new Promise(r => setTimeout(r, 1100)) })
    expect(onExpire).toHaveBeenCalledOnce()
  })
})
