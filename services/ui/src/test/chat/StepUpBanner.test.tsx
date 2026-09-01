import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StepUpBanner } from '../../components/chat/StepUpBanner'

const props = {
  tool: 'apply_change',
  context: { device_host: 'router-core-01' },
  approverType: 'designated' as const,
  expiresAt: new Date(Date.now() + 900_000).toISOString(),  // 15 min from now
  onApprove: vi.fn(),
  onReject: vi.fn(),
}

describe('StepUpBanner', () => {
  it('renders tool and device host from context', () => {
    render(<StepUpBanner {...props} />)
    expect(screen.getByText(/apply_change/)).toBeInTheDocument()
    expect(screen.getByText(/router-core-01/)).toBeInTheDocument()
  })

  it('renders change_id descriptor when device_host is absent', () => {
    render(<StepUpBanner {...props} context={{ change_id: 'chg-42' }} />)
    expect(screen.getByText(/change chg-42/)).toBeInTheDocument()
  })

  it('shows "designated approver required" for approver_type designated', () => {
    render(<StepUpBanner {...props} approverType="designated" />)
    expect(screen.getByText(/designated approver required/i)).toBeInTheDocument()
  })

  it('shows "self-approval allowed" for approver_type self', () => {
    render(<StepUpBanner {...props} approverType="self" />)
    expect(screen.getByText(/self-approval allowed/i)).toBeInTheDocument()
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
