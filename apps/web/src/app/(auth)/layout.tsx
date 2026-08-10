export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="grid min-h-[calc(100vh-2.5rem)] place-items-center px-4 bg-gradient-to-b from-slate-50 via-white to-brand-50/30">
      <div className="w-full max-w-sm animate-fade-in">
        {/* Logo */}
        <div className="mb-8 flex flex-col items-center">
          {/* Static SVG logo — next/image does not optimize SVG, so <img> is correct here. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/logo.svg" alt="Aether Clinician" className="mb-4 h-14 w-14" />
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">
            Aether <span className="text-brand-600">Clinician</span>
          </h1>
          <p className="mt-1.5 text-sm text-slate-500">
            Diagnostic & management decision support
          </p>
        </div>
        {children}
      </div>
    </main>
  );
}
