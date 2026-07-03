"""
Reporter Module
---------------
Architectural Purpose:
This module is strictly responsible for output formatting. Isolating it ensures that if 
we ever want to build a web dashboard, generate PDF reports, or connect to Tableau, 
we only have to edit this file, rather than digging through the optimization mathematics.
"""
import pandas as pd
from core.schema import ResultCols

def save_report(excel_path, res_df, fin_df):
    print(f"\n💾 Saving results back to {excel_path}...")
    try:
        xls = pd.ExcelFile(excel_path)
        sheets = {sheet: pd.read_excel(xls, sheet_name=sheet) for sheet in xls.sheet_names}
        
        # Clean up data granularity: Round all floats to 2 decimal places
        # This ensures we export clean numerical points instead of long trailing decimals
        res_df = res_df.round(2)
        if 'Value' in fin_df.columns:
            fin_df['Value'] = fin_df['Value'].apply(lambda x: round(x, 2) if isinstance(x, (int, float)) else x)
            
        # Pivot Financial Summary to create a side-by-side Scenario Comparison Table
        fin_pivot = fin_df.pivot(index='Metric', columns=ResultCols.SCENARIO, values='Value').reset_index()
        
        sheets['Optimization Results'] = res_df
        sheets['Financial Summary'] = fin_pivot
        
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            for sheet_name, df in sheets.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                
        print("✅ Results successfully saved to the Excel file.")
    except Exception as e:
        print(f"❌ Failed to save results: {str(e)}")
