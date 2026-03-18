interface Props {
  label: string
  value: number
  valueClass: string
  glowAmber?: boolean
  formatK?: boolean
}

export function StatTile({ label, value, valueClass, glowAmber, formatK }: Props) {
  const display = formatK ? `${Math.round(value / 1000)}k` : String(value)
  return (
    <div
      className={`bg-vigil-card border rounded p-4 text-center flex-1 ${
        glowAmber ? 'border-amber-500/40' : 'border-vigil-border'
      }`}
    >
      <div className={`text-3xl font-bold ${valueClass}`}>{display}</div>
      <div className="text-xs text-vigil-muted uppercase tracking-widest mt-1">{label}</div>
    </div>
  )
}
