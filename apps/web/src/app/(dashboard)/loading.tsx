export default function DashboardLoading() {
  return (
    <div className="flex items-center justify-center py-24">
      <div className="text-center">
        <div className="relative mx-auto mb-4 h-10 w-10">
          <div className="absolute inset-0 animate-ping rounded-full bg-brand-200 opacity-75" />
          <div className="relative flex h-10 w-10 items-center justify-center rounded-full bg-brand-100">
            <svg
              className="h-5 w-5 animate-spin text-brand-600"
              viewBox="0 0 24 24"
              fill="none"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
          </div>
        </div>
        <p className="text-sm font-medium text-slate-600">Loading...</p>
        <p className="mt-0.5 text-xs text-slate-400">Please wait a moment</p>
      </div>
    </div>
  );
}
