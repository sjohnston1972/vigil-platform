import { render, screen } from '@testing-library/react'
import { StatTile } from '../../components/dashboard/StatTile'

describe('StatTile', () => {
  it('renders value and label', () => {
    render(<StatTile label="Active" value={3} valueClass="text-green-500" />)
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('ACTIVE')).toBeInTheDocument()
  })

  it('applies valueClass to the value element', () => {
    render(<StatTile label="Failures" value={2} valueClass="text-red-500" glowAmber={false} />)
    expect(screen.getByText('2')).toHaveClass('text-red-500')
  })

  it('formats large numbers with k suffix', () => {
    render(<StatTile label="Tokens" value={42180} valueClass="text-vigil-accent-text" formatK />)
    expect(screen.getByText('42k')).toBeInTheDocument()
  })
})
