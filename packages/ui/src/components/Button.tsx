export function Button({
  children,
  className = '',
  variant = 'primary',
  size = 'default',
  // HTML defaults an unspecified button inside a form to type="submit". Every submit button
  // in this app says so explicitly, so defaulting to "button" here means a control dropped
  // into a form later cannot silently submit it.
  type = 'button',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'default' | 'lg';
}) {
  const baseStyles =
    'inline-flex items-center justify-center font-medium transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2';

  const variantStyles = {
    primary:
      'bg-brand-600 text-white hover:bg-brand-700 active:bg-brand-800 focus-visible:ring-brand-500 shadow-sm hover:shadow',
    secondary:
      'bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 hover:border-slate-400 active:bg-slate-100 focus-visible:ring-brand-500 shadow-sm',
    danger:
      'bg-red-600 text-white hover:bg-red-700 active:bg-red-800 focus-visible:ring-red-500 shadow-sm hover:shadow',
    ghost:
      'text-slate-600 hover:text-slate-900 hover:bg-slate-100 active:bg-slate-200 focus-visible:ring-brand-500',
  }[variant];

  const sizeStyles = {
    sm: 'rounded-lg px-3 py-1.5 text-xs gap-1.5',
    default: 'rounded-lg px-4 py-2.5 text-sm gap-2',
    lg: 'rounded-xl px-6 py-3 text-base gap-2.5',
  }[size];

  return (
    <button
      type={type}
      className={`${baseStyles} ${variantStyles} ${sizeStyles} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
