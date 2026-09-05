import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { startTelemetry, track } from './telemetry'

// Start client telemetry ASAP so even boot-time errors are captured server-side.
startTelemetry()

// Error boundary: a render crash logs to the server (and shows a message)
// instead of leaving a silent blank page.
class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    track('react_error_boundary', {
      message: error.message,
      stack: (error.stack ?? '').slice(0, 2000),
      componentStack: (info.componentStack ?? '').slice(0, 2000),
    }, 'error')
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: 'monospace', color: '#f88' }}>
          <h2>UI error (logged to server)</h2>
          <pre style={{ whiteSpace: 'pre-wrap' }}>{this.state.error.message}</pre>
          <button onClick={() => location.reload()}>Reload</button>
        </div>
      )
    }
    return this.props.children
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
