import { useState } from 'react'

// Dashboard shell. The five views are built out in Phase 6; this establishes
// the navigation skeleton and the dark-mode layout the panels render into.
const VIEWS = ['Overview', 'Predictions', 'Playbooks', 'Evolution', 'Approvals']

export default function App() {
  const [active, setActive] = useState(VIEWS[0])

  return (
    <div className="flex min-h-screen">
      <nav className="w-56 shrink-0 border-r border-white/10 p-4">
        <h1 className="mb-6 text-lg font-semibold tracking-tight">NEXUS</h1>
        <ul className="space-y-1">
          {VIEWS.map((view) => (
            <li key={view}>
              <button
                type="button"
                onClick={() => setActive(view)}
                className={`w-full rounded px-3 py-2 text-left text-sm ${
                  active === view ? 'bg-white/10 font-medium' : 'text-white/60 hover:bg-white/5'
                }`}
              >
                {view}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <main className="flex-1 p-8">
        <h2 className="text-2xl font-semibold tracking-tight">{active}</h2>
        <p className="mt-2 text-sm text-white/50">
          Not yet implemented. Reads land on the dashboard API Lambda using follower reads.
        </p>
      </main>
    </div>
  )
}
