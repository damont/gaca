import type { InputHTMLAttributes } from "react";

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  id: string;
}

/**
 * A labelled text input.
 *
 * Extending InputHTMLAttributes and spreading `...rest` means this accepts
 * every normal input prop — type, value, onChange, required, autoComplete —
 * without re-declaring any of them. That is the usual way to wrap a native
 * element without shrinking what it can do.
 */
export default function Field({ label, id, className = "", ...rest }: FieldProps) {
  return (
    <label htmlFor={id} className="block">
      <span className="mb-1 block text-sm font-medium text-text-secondary">
        {label}
      </span>
      <input
        id={id}
        className={
          "w-full rounded-md border border-border bg-surface px-3 py-2 text-text-primary " +
          "outline-none transition focus:border-primary-500 focus:ring-2 focus:ring-primary-100 " +
          className
        }
        {...rest}
      />
    </label>
  );
}
